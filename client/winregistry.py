"""Capture Windows registry keys into the portable model (Windows only).

Walks each manifest registry key recursively and records its values and subkeys.
Imported lazily so the module is safe to load on Linux (capture is a no-op there).
"""
from __future__ import annotations

from shared.registry import RegKey, RegValue, parse_manifest_key

try:
    import winreg  # type: ignore
    _ROOT_HANDLES = {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
        "HKCC": winreg.HKEY_CURRENT_CONFIG,
    }
    _TYPE_NAMES = {
        winreg.REG_SZ: "REG_SZ",
        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
        winreg.REG_DWORD: "REG_DWORD",
        winreg.REG_QWORD: "REG_QWORD",
        winreg.REG_BINARY: "REG_BINARY",
        winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
    }
    HAVE_WINREG = True
except ImportError:  # not on Windows
    winreg = None  # type: ignore
    _ROOT_HANDLES = {}
    _TYPE_NAMES = {}
    HAVE_WINREG = False


def available() -> bool:
    return HAVE_WINREG


def capture_keys(manifest_keys: list[str]) -> list[RegKey]:
    """Capture each manifest registry key (recursively) into RegKey objects.

    Missing keys are skipped silently (the game may not be installed / run yet).
    """
    if not HAVE_WINREG:
        return []
    out: list[RegKey] = []
    for mkey in manifest_keys:
        try:
            root, rel = parse_manifest_key(mkey)
        except ValueError:
            continue
        handle = _ROOT_HANDLES.get(root)
        if handle is None:
            continue
        _walk(handle, root, rel.replace("/", "\\"), out)
    return out


def _walk(root_handle, root_code: str, subpath: str, out: list[RegKey]) -> None:
    try:
        key = winreg.OpenKey(root_handle, subpath, 0, winreg.KEY_READ)
    except OSError:
        return
    with key:
        values = []
        i = 0
        while True:
            try:
                name, data, vtype = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            values.append(RegValue(name=name, type=_TYPE_NAMES.get(vtype, "REG_SZ"),
                                    data=_encode(data, vtype)))
        out.append(RegKey(root=root_code, path=subpath.replace("\\", "/"), values=values))

        # Recurse into subkeys.
        j = 0
        while True:
            try:
                child = winreg.EnumKey(key, j)
            except OSError:
                break
            j += 1
            _walk(root_handle, root_code, f"{subpath}\\{child}", out)


def _encode(data, vtype):
    """Convert a winreg value into the JSON-friendly form used by RegValue."""
    if vtype == winreg.REG_BINARY:
        return (data or b"").hex()
    if vtype == winreg.REG_MULTI_SZ:
        return list(data or [])
    if vtype in (winreg.REG_DWORD, winreg.REG_QWORD):
        return int(data)
    return data  # strings pass through
