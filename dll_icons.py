"""从图标库 DLL 里取图标：按资源顺序（和资源管理器"更改图标"里的序号一致）拼成 .ico 字节。

Windows 的 DLL 里图标分两层：RT_GROUP_ICON 记录一组尺寸，每个尺寸的位图单独存成 RT_ICON。
把它们按 .ico 文件格式拼回去，Pillow 就能直接打开。
"""

import ctypes
import io
import struct
from ctypes import wintypes
from pathlib import Path

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.LoadLibraryExW.restype = wintypes.HMODULE
_k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
_k32.FindResourceW.restype = wintypes.HRSRC
_k32.FindResourceW.argtypes = [wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID]
_k32.LoadResource.restype = wintypes.HGLOBAL
_k32.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
_k32.LockResource.restype = wintypes.LPVOID
_k32.LockResource.argtypes = [wintypes.HGLOBAL]
_k32.SizeofResource.restype = wintypes.DWORD
_k32.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
_k32.FreeLibrary.argtypes = [wintypes.HMODULE]

RT_ICON = 3
RT_GROUP_ICON = 14
_AS_DATAFILE = 0x00000002 | 0x00000020  # LOAD_LIBRARY_AS_DATAFILE | AS_IMAGE_RESOURCE

_ENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPARAM
)
_k32.EnumResourceNamesW.argtypes = [
    wintypes.HMODULE,
    wintypes.LPVOID,
    _ENUMPROC,
    wintypes.LPARAM,
]


def _res(mod, name, rtype) -> bytes:
    h = _k32.FindResourceW(mod, name, rtype)
    if not h:
        raise OSError(f"找不到资源 {name}")
    ptr = _k32.LockResource(_k32.LoadResource(mod, h))
    return ctypes.string_at(ptr, _k32.SizeofResource(mod, h))


def _group_names(mod) -> list:
    """所有图标组的名字，保持 DLL 里的顺序。名字可能是整数 ID，也可能是字符串"""
    names = []

    def cb(_m, _t, name, _p):
        names.append(name if name < 0x10000 else ctypes.wstring_at(name))
        return True

    _k32.EnumResourceNamesW(mod, RT_GROUP_ICON, _ENUMPROC(cb), 0)
    return names


def _build_ico(mod, group) -> bytes:
    """GRPICONDIR 和 ICONDIR 只差最后一个字段：DLL 里是 RT_ICON 的 ID，.ico 里是数据偏移"""
    grp = _res(mod, group, RT_GROUP_ICON)
    _, _, count = struct.unpack_from("<HHH", grp, 0)
    entries, blobs = [], []
    for i in range(count):
        w, h, colors, _, planes, bits, size, icon_id = struct.unpack_from(
            "<BBBBHHIH", grp, 6 + i * 14
        )
        blob = _res(mod, icon_id, RT_ICON)
        entries.append((w, h, colors, planes, bits, len(blob)))
        blobs.append(blob)
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, count))
    offset = 6 + 16 * count
    for (w, h, colors, planes, bits, size), blob in zip(entries, blobs):
        out.write(struct.pack("<BBBBHHII", w, h, colors, 0, planes, bits, size, offset))
        offset += size
    for blob in blobs:
        out.write(blob)
    return out.getvalue()


def _name_arg(name):
    return ctypes.c_void_p(name) if isinstance(name, int) else ctypes.c_wchar_p(name)


def all_icons(dll: Path) -> list[bytes]:
    """DLL 里每个图标组的 .ico 字节，下标就是快捷方式里用的图标序号"""
    mod = _k32.LoadLibraryExW(str(dll), None, _AS_DATAFILE)
    if not mod:
        raise OSError(f"打不开 {dll}")
    try:
        return [_build_ico(mod, _name_arg(n)) for n in _group_names(mod)]
    finally:
        _k32.FreeLibrary(mod)


def icon_bytes(dll: Path, index: int) -> bytes:
    """第 index 个图标的 .ico 字节"""
    return all_icons(dll)[index]
