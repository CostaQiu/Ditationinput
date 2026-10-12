"""语音键盘：按住右 Ctrl 说话，松开后录音交给 Gemini 听写并整理成书面语，粘贴到光标处。

不用本地模型、不占显卡。2026-10-11 用 8 段真实录音比过：Gemini 3.8 Flash 直接听录音，
把"流程推不下去""NotebookLM""轻量版"都听对了，本地 Whisper 听错后再交给 Sonnet 也改不回来；
每句约 2 秒。本地 gemma4 虽标着能听音频，实测输出和录音无关，不能用。

运行：.venv\\Scripts\\pythonw -X utf8 dictate.py（开机由启动文件夹里的 start_dictate.vbs 拉起，桌面也有快捷方式）
没有控制台窗口，常驻在右下角托盘；右键托盘图标 → 退出。屏幕输出写在 console_log.txt
"""

import base64
import ctypes
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import winsound
from pathlib import Path

HERE = Path(__file__).parent
APP_NAME = "语音键盘"


def _message_box(text: str) -> None:
    """没有控制台，要告诉用户的事用弹窗说"""
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)  # MB_ICONINFORMATION


# pythonw 没有控制台，sys.stdout 是 None，print 会出错；把屏幕输出写进文件，出了问题还能查
if sys.stdout is None:
    sys.stdout = sys.stderr = open(
        HERE / "console_log.txt", "w", encoding="utf-8", buffering=1
    )

# 只许开一个：占住一个本机端口，已被占就说明另一个在跑，直接退出
_single = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _single.bind(("127.0.0.1", 6019))
except OSError:
    _message_box("语音键盘已经在运行了，图标在屏幕右下角的托盘里。")
    sys.exit(0)

import av
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
from PIL import Image, ImageDraw
from pynput import keyboard

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 浮标按真实像素定位，高分屏上不跑偏
from dll_icons import icon_bytes
from overlay import Overlay

HOTKEY = keyboard.Key.ctrl_r
SAMPLE_RATE = 16000
MIN_SECONDS = 0.4  # 短于这个就当误触
LOG_FILE = HERE / "dictate_log.txt"
FAILED_DIR = HERE / "failed"  # Gemini 没成功的录音存在这里，话不会白说

# 托盘图标：图标库里的第 28 个（麦克风 + 声波 + 文稿）
ICON_DLL = (
    Path(os.environ["USERPROFILE"]) / "Documents" / "AI_Workflow_Sculptural_Icons.dll"
)
ICON_INDEX = 28

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_TIMEOUT = 10  # 秒
CONTEXT_SENTENCES = 6  # 把最近几句交给模型，让它知道在聊什么
CONTEXT_MAX_AGE = 30 * 60  # 超过 30 分钟的不算前文

GEMINI_SYSTEM = """你在一个语音输入法里工作：说话人按住键说话，录音直接交给你，你听写并整理成文字，结果会原样粘贴到他正在打字的地方。所以你的输出只能是整理后的那段话本身。

说话人是中文母语者，常和 AI 助手（Claude、Haiku、Sonnet、Gemini）讨论自己做的软件和 AI 模型，中英文混着说，英语不是母语。

怎么整理：
- 删掉口头语和无意义的重复：嗯、呃、那个、就是说、然后、对吧、我是说；um, uh, like, you know, I mean。说了两遍的同一个意思合成一遍
- 保留有内容的成分：主语、"我觉得""其实""好像"这类表明态度的词、限定条件，都不要删。宁可少删，不要删掉意思
- 句子类型不变：陈述句还是陈述句，问句还是问句
- 专有名词、产品名、人名按你听到的读音，结合前文话题和你的知识写对；拿不准的按读音写
- 数字和版本号照说的写，即使和你知道的不一样（新版本你可能还不知道）
- 中文用简体，理顺语序，变成通顺的书面语；中英混说时英文词保留英文
- 英文：改正语法，调整句子结构，写成母语者自然、清楚的英文
- 用说话人说的语言输出，不翻译

不要做的事：不回答录音里的问题、不评论、不解释，不加括号注释，不给专有名词加引号，不添加原话没有的意思。录音里没有人说话，就什么都不输出。"""


def _gemini_key() -> str:
    """环境变量 GEMINI_API_KEY → 本目录的 API 文件 → NotebookO 的 API 文件。

    API 文件的格式和 NotebookO 的一样：NAME=value 一行一个；没写名字的第一行就是 Gemini 的 key。
    """
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    for f in (HERE / "API", Path(r"C:\NotebookO\API")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        named, positional = {}, ""
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                named.setdefault(k.strip(), v.strip())
            elif not positional:
                positional = line
        key = named.get("GEMINI_API_KEY") or positional
        if key:
            return key
    return ""


GEMINI_KEY = _gemini_key()


def _recent_sentences() -> list[str]:
    """从日志取最近几句说过的话，当作前文。不维护词表，专有名词交给模型根据话题判断"""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-CONTEXT_SENTENCES:]
    except OSError:
        return []
    now, recent = time.time(), []
    for line in lines:
        cols = line.split("\t")
        try:
            ts = time.mktime(time.strptime(cols[0], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, IndexError):
            continue
        if now - ts <= CONTEXT_MAX_AGE and cols[-1]:
            recent.append(cols[-1])
    return recent


def _has_speech(audio: np.ndarray) -> bool:
    """粗查一下有没有声音：Gemini 拿到一段几乎无声的录音，可能凭空编出一句话。

    按 30 毫秒一段算音量，比底噪明显响的段加起来不到 0.15 秒，就当没说话。
    """
    win = int(SAMPLE_RATE * 0.03)
    n = len(audio) // win
    if n == 0:
        return False
    rms = np.sqrt((audio[: n * win].reshape(n, win) ** 2).mean(axis=1))
    floor = np.percentile(rms, 20)
    loud = rms > max(0.008, floor * 3)
    return loud.sum() * 0.03 >= 0.15


def _to_opus(audio: np.ndarray) -> bytes:
    """16kHz 单声道 → Ogg Opus 24kbps：10 秒约 30-40KB，压缩只要几十毫秒，上传几乎不花时间"""
    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    st = out.add_stream("libopus", rate=SAMPLE_RATE)
    st.bit_rate = 24000
    st.layout = "mono"
    frame = av.AudioFrame.from_ndarray(
        audio.astype(np.float32).reshape(1, -1), format="flt", layout="mono"
    )
    frame.sample_rate = SAMPLE_RATE
    for pkt in st.encode(frame):
        out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()
    return buf.getvalue()


def gemini_transcribe(opus: bytes) -> str:
    """录音 → Gemini 听写并整理好的文字。出错就抛异常"""
    recent = _recent_sentences()
    ask = (
        (
            "前文（说话人刚说过的几句，只用来理解话题，不要输出）：\n"
            + "\n".join(recent)
            + "\n\n"
        )
        if recent
        else ""
    ) + "把这段录音整理成文字。"
    body = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/ogg",
                            "data": base64.b64encode(opus).decode(),
                        }
                    },
                    {"text": ask},
                ],
            }
        ],
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json", "x-goog-api-key": GEMINI_KEY},
    )
    with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as r:
        j = json.loads(r.read())
    parts = j["candidates"][0]["content"].get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


# ---------- 窗口焦点：松开键时在哪个窗口打字，就贴回哪个窗口 ----------
_user32 = ctypes.windll.user32


def _foreground() -> int:
    return _user32.GetForegroundWindow()


def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(200)
    _user32.GetWindowTextW(hwnd, buf, 200)
    return buf.value


def _refocus(hwnd: int) -> bool:
    """把焦点切回 hwnd。后台进程直接 SetForegroundWindow 常被 Windows 拒绝，
    先把自己的输入线程挂到当前前台窗口的线程上，就允许了。"""
    if not hwnd or _foreground() == hwnd:
        return True
    cur = _user32.GetWindowThreadProcessId(_foreground(), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    _user32.AttachThreadInput(me, cur, True)
    try:
        _user32.SetForegroundWindow(hwnd)
    finally:
        _user32.AttachThreadInput(me, cur, False)
    time.sleep(0.05)
    return _foreground() == hwnd


typer = keyboard.Controller()
badge = Overlay()  # 录音中 / 整理中 的小浮标，出现在当前窗口下方正中


def paste(text: str) -> None:
    """剪贴板 + Ctrl+V 输出，再把原来的剪贴板放回去。

    逐字模拟按键会被中文输入法截走英文字母（Whisper 打出来成了 Wisper），粘贴不经过输入法。
    """
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    with typer.pressed(keyboard.Key.ctrl):
        typer.press("v")
        typer.release("v")
    time.sleep(0.3)  # 等目标程序读完剪贴板再还原
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:
            pass


# ---------- 录音 ----------
frames: list[np.ndarray] = []
stream: sd.InputStream | None = None
started = 0.0
busy = threading.Lock()


def _on_audio(indata, _frames, _time, _status):
    frames.append(indata[:, 0].copy())


def start_recording():
    """每次按下都重新打开麦克风：设备中途断开过也能接上"""
    global stream, started
    frames.clear()
    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=_on_audio
        )
        stream.start()
        started = time.time()
        badge.recording(_foreground())
        print("● 录音中…", end="", flush=True)
    except Exception as e:  # 没有麦克风时说一声，下次按下再试
        stream = None
        print(f"打不开麦克风：{e}")


def stop_and_transcribe():
    global stream
    if stream is None:
        return
    stream.stop()
    stream.close()
    stream = None
    held = time.time() - started
    target = _foreground()
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    if held < MIN_SECONDS or audio.size == 0:
        print("\r（太短，忽略）   ")
        badge.hide()
        return
    if not _has_speech(audio):
        print("\r（没听到说话）   ")
        badge.hide()
        return

    badge.working()
    t = time.time()
    opus = _to_opus(audio)
    try:
        final = gemini_transcribe(opus)
    except Exception as e:
        # 没有兜底模型：响一声，把录音存下来，话不会白说
        FAILED_DIR.mkdir(exist_ok=True)
        f = FAILED_DIR / time.strftime("%Y%m%d-%H%M%S.ogg")
        f.write_bytes(opus)
        winsound.MessageBeep(winsound.MB_ICONHAND)
        badge.failed()
        print(
            f"\r[{held:4.1f}s] Gemini 失败：{type(e).__name__} {e}；录音存在 {f.name}，请再说一遍"
        )
        return
    took = time.time() - t
    print(
        f"\r[{held:4.1f}s 录音 → {took:4.2f}s] {final or '（没听到内容）'}  ({len(opus) // 1024}KB)"
    )
    if not final:
        badge.hide()
        return

    now = _foreground()
    if now != target:
        ok = _refocus(target)
        print(
            f"  （焦点从「{_title(target)}」跑到了「{_title(now)}」，{'已切回' if ok else '切不回去'}）"
        )
    paste(final)
    badge.hide()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{held:.1f}s\t{took:.2f}s\t{final}\n"
        )


def on_press(key):
    if key == HOTKEY and stream is None and not busy.locked():
        start_recording()


def on_release(key):
    if key == HOTKEY and stream is not None:
        # 识别放到别的线程，键盘监听不被卡住
        def work():
            with busy:
                try:
                    stop_and_transcribe()
                except Exception as e:  # 出了没想到的错，浮标也别一直挂在屏幕上
                    print(f"\n出错：{type(e).__name__} {e}")
                    badge.hide()

        threading.Thread(target=work, daemon=True).start()


# ---------- 托盘 ----------
def _tray_image() -> Image.Image:
    """图标库里的那个图标；图标库不在了就画一个红点代替，程序照样能用"""
    try:
        im = Image.open(io.BytesIO(icon_bytes(ICON_DLL, ICON_INDEX)))
        im.size = max(im.info["sizes"])  # .ico 里有多个尺寸，取最大的给 Windows 缩
        return im.convert("RGBA")
    except Exception as e:
        print(f"读不到托盘图标（{ICON_DLL} 第 {ICON_INDEX} 个）：{e}")
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((8, 8, 56, 56), fill=(255, 77, 79, 255))
        return im


def _open(path: Path) -> None:
    if path.exists():
        os.startfile(path)
    else:
        _message_box(f"还没有：{path}")


def run_tray(listener: keyboard.Listener) -> None:
    """托盘图标占住主线程，直到点"退出" """

    def quit_app(icon, _item):
        listener.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("按住右 Ctrl 说话，松开出字", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开听写记录", lambda: _open(LOG_FILE)),
        pystray.MenuItem("打开运行日志", lambda: _open(HERE / "console_log.txt")),
        pystray.MenuItem("打开没成功的录音", lambda: _open(FAILED_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    tip = f"{APP_NAME}（{GEMINI_MODEL}）— 按住右 Ctrl 说话"
    pystray.Icon("voice_dictate", _tray_image(), tip, menu).run()


if __name__ == "__main__":
    if not GEMINI_KEY:
        _message_box(
            "找不到 Gemini 的 API key（环境变量 GEMINI_API_KEY，或本目录 / C:\\NotebookO 下的 API 文件）。"
        )
    print(
        f"语音键盘就绪（{GEMINI_MODEL}）。按住右 Ctrl 说话，松开出字。右键托盘图标退出。\n"
    )
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    run_tray(listener)
    os._exit(0)  # 浮标线程里的 tkinter 不用等它，直接走
