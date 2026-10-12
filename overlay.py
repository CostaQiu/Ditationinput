"""状态小浮标：录音时一个红点，整理时一个转圈，出现在当前窗口的下方正中。

它不能抢焦点——字是靠 Ctrl+V 贴进当前窗口的，浮标一拿到焦点，字就贴到它身上去了。
所以窗口带 WS_EX_NOACTIVATE（点不着、不激活）和 WS_EX_TRANSPARENT（鼠标穿透），
空闲时也不关掉，只把透明度降到 0，免得每次显示都走一遍"显示窗口"那条会激活的路。

tkinter 只能在一个线程里用，所以浮标有自己的线程，别的线程通过队列发指令。
"""

import ctypes
import math
import queue
import threading
import tkinter as tk

_user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

KEY = "#010203"  # 透明色：画在这个颜色上的地方都看不见
BG = "#1f1f23"
FG = "#e8e8ea"
RED = "#ff4d4f"
ACCENT = "#5b9dff"

W, H = 156, 40  # 96 DPI 下的尺寸，按目标窗口的 DPI 放大


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def _place_for(hwnd: int, w: int, h: int) -> tuple[int, int]:
    """目标窗口下方正中、离底边一点的位置；窗口拿不到就用主屏。限制在屏幕工作区内。"""
    r = _RECT()
    if hwnd:
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
    mon = _user32.MonitorFromWindow(hwnd, 1)  # 1 = 拿不到就用主屏
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    work = mi.rcWork
    if not hwnd or r.right <= r.left:
        r = work
    x = (r.left + r.right) // 2 - w // 2
    y = r.bottom - h - int(h * 1.6)
    x = max(work.left + 8, min(x, work.right - w - 8))
    y = max(work.top + 8, min(y, work.bottom - h - 8))
    return x, y


class Overlay:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._state = "idle"
        self._angle = 0
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    # ---- 给别的线程用的接口 ----
    def recording(self, hwnd: int):
        self._q.put(("recording", hwnd))

    def working(self):
        self._q.put(("working", None))

    def failed(self):
        self._q.put(("failed", None))

    def hide(self):
        self._q.put(("idle", None))

    # ---- 浮标线程 ----
    def _run(self):
        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        root.geometry(f"{W}x{H}+0+0")
        root.update_idletasks()
        self.hwnd = _user32.GetParent(root.winfo_id())
        ex = _user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(
            self.hwnd,
            GWL_EXSTYLE,
            ex
            | WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOOLWINDOW
            | WS_EX_TOPMOST
            | WS_EX_NOACTIVATE,
        )
        self.scale = 1.0
        self._ready.set()
        root.after(30, self._tick)
        root.mainloop()

    def _show_at(self, hwnd: int):
        dpi = _user32.GetDpiForWindow(hwnd) if hwnd else 96
        self.scale = (dpi or 96) / 96
        w, h = int(W * self.scale), int(H * self.scale)
        x, y = _place_for(hwnd, w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        _user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
        self.root.attributes("-alpha", 0.95)

    def _tick(self):
        try:
            while True:
                cmd, arg = self._q.get_nowait()
                if cmd == "recording":
                    self._show_at(arg)
                elif cmd == "idle":
                    self.root.attributes("-alpha", 0.0)
                elif cmd == "failed":
                    self._fail_until = self._frame + 50  # 约 1.5 秒后自动隐藏
                self._state = cmd
        except queue.Empty:
            pass
        self._frame = getattr(self, "_frame", 0) + 1
        if self._state == "failed" and self._frame >= self._fail_until:
            self._state = "idle"
            self.root.attributes("-alpha", 0.0)
        if self._state != "idle":
            self._draw()
        self.root.after(30, self._tick)

    def _draw(self):
        c, s = self.canvas, self.scale
        c.delete("all")
        w, h = int(W * s), int(H * s)
        r = h // 2
        # 胶囊底
        c.create_oval(0, 0, h, h, fill=BG, outline=BG)
        c.create_oval(w - h, 0, w, h, fill=BG, outline=BG)
        c.create_rectangle(r, 0, w - r, h, fill=BG, outline=BG)
        cx, cy, ir = r + int(4 * s), r, int(8 * s)
        font = ("Microsoft YaHei UI", 10)
        if self._state == "recording":
            # 红点一明一暗
            k = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._frame / 5))
            col = "#%02x%02x%02x" % (int(255 * k), int(77 * k), int(79 * k))
            c.create_oval(
                cx - ir + 2,
                cy - ir + 2,
                cx + ir - 2,
                cy + ir - 2,
                fill=col,
                outline=col,
            )
            text = "录音中"
        elif self._state == "working":
            self._angle = (self._angle - 12) % 360
            w_line = max(2, int(2.5 * s))
            c.create_oval(
                cx - ir, cy - ir, cx + ir, cy + ir, outline="#3a3a40", width=w_line
            )
            c.create_arc(
                cx - ir,
                cy - ir,
                cx + ir,
                cy + ir,
                start=self._angle,
                extent=100,
                style="arc",
                outline=ACCENT,
                width=w_line,
            )
            text = "整理中"
        else:  # failed
            c.create_oval(
                cx - ir + 2,
                cy - ir + 2,
                cx + ir - 2,
                cy + ir - 2,
                fill=RED,
                outline=RED,
            )
            text = "没成功，请重说"
        c.create_text(
            cx + ir + int(10 * s), cy, text=text, fill=FG, font=font, anchor="w"
        )


if __name__ == "__main__":
    # 单独跑一遍看样子：录音 2 秒 → 整理 3 秒 → 失败提示
    import time

    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    o = Overlay()
    o.recording(_user32.GetForegroundWindow())
    time.sleep(2)
    o.working()
    time.sleep(3)
    o.failed()
    time.sleep(2)
