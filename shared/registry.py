"""Portable Windows-registry model + Wine ``.reg`` rendering.

Some games keep saves/config in the Windows registry. To migrate those to a
Proton prefix we (1) capture the relevant keys on Windows into an OS-neutral JSON
model, then (2) render them into the prefix's ``user.reg`` / ``system.reg`` (Wine
registry text format).

Value ``data`` is stored in JSON-friendly forms:
    REG_SZ / REG_EXPAND_SZ -> str
    REG_DWORD / REG_QWORD   -> int
    REG_BINARY             -> hex str (e.g. "deadbeef")
    REG_MULTI_SZ           -> list[str]
"""
from __future__ import annotations

import dataclasses
import time

# Manifest hive prefixes -> short root code.
_HIVE_PREFIX = {
    "HKEY_CURRENT_USER": "HKCU",
    "HKEY_LOCAL_MACHINE": "HKLM",
    "HKEY_CLASSES_ROOT": "HKCR",
    "HKEY_USERS": "HKU",
    "HKEY_CURRENT_CONFIG": "HKCC",
    # Allow short forms too.
    "HKCU": "HKCU", "HKLM": "HKLM", "HKCR": "HKCR", "HKU": "HKU", "HKCC": "HKCC",
}

# Which Wine .reg file each root lives in, and the (forward-slash) path prefix
# inside that file. Wine keeps HKCU in user.reg and HKLM in system.reg (both
# relative to the root); HKCR is mapped under HKLM\Software\Classes.
_ROOT_TO_HIVE = {
    "HKCU": ("user", ""),
    "HKLM": ("system", ""),
    "HKCR": ("system", "Software/Classes/"),
}


@dataclasses.dataclass
class RegValue:
    name: str           # "" means the key's default value
    type: str           # REG_SZ, REG_DWORD, ...
    data: object

    def to_wine(self) -> str:
        """Render one ``"name"=...`` line in Wine/.reg syntax."""
        lhs = "@" if self.name == "" else f'"{_esc(self.name)}"'
        return f"{lhs}={self._render_value()}"

    def _render_value(self) -> str:
        t = self.type
        if t in ("REG_SZ",):
            return f'"{_esc(str(self.data))}"'
        if t == "REG_DWORD":
            return f"dword:{int(self.data) & 0xFFFFFFFF:08x}"
        if t == "REG_QWORD":
            return "hex(b):" + _hex_bytes(int(self.data).to_bytes(8, "little"))
        if t == "REG_BINARY":
            raw = bytes.fromhex(str(self.data)) if self.data else b""
            return "hex:" + _hex_bytes(raw)
        if t == "REG_EXPAND_SZ":
            raw = (str(self.data)).encode("utf-16-le") + b"\x00\x00"
            return "hex(2):" + _hex_bytes(raw)
        if t == "REG_MULTI_SZ":
            parts = list(self.data or [])
            raw = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in parts) + b"\x00\x00"
            return "hex(7):" + _hex_bytes(raw)
        # Unknown type: store as opaque binary if we can, else empty string.
        return '""'


@dataclasses.dataclass
class RegKey:
    root: str               # HKCU | HKLM | HKCR | ...
    path: str               # forward-slash path relative to root, e.g. "SOFTWARE/Celeste"
    values: list[RegValue] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "path": self.path,
            "values": [dataclasses.asdict(v) for v in self.values],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RegKey":
        return cls(
            root=d["root"], path=d["path"],
            values=[RegValue(**v) for v in d.get("values", [])],
        )


def parse_manifest_key(key: str) -> tuple[str, str]:
    """Split a manifest registry key into (root_code, rel_path).

    e.g. "HKEY_CURRENT_USER/SOFTWARE/Celeste" -> ("HKCU", "SOFTWARE/Celeste")
    """
    norm = key.replace("\\", "/").strip("/")
    head, _, rest = norm.partition("/")
    root = _HIVE_PREFIX.get(head.upper())
    if not root:
        raise ValueError(f"Unknown registry hive in key: {key!r}")
    return root, rest


def render_hive(keys: list[RegKey], hive: str) -> str:
    """Render the keys destined for one hive ("user" or "system") as a Wine
    ``.reg`` fragment (without the file header). Empty string if no keys apply.
    """
    blocks: list[str] = []
    ts = int(time.time())
    for k in keys:
        mapping = _ROOT_TO_HIVE.get(k.root)
        if not mapping or mapping[0] != hive:
            continue
        prefix = mapping[1]
        # Wine .reg section headers separate path components with "\\" (an
        # escaped backslash), e.g. [Software\\Celeste].
        logical = (prefix + k.path).strip("/")
        section = logical.replace("/", "\\\\")
        lines = [f"[{section}] {ts}"]
        for v in k.values:
            lines.append(v.to_wine())
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def render_windows_reg(keys: list[RegKey]) -> str:
    """Render a standard Windows ``.reg`` file (Version 5.00) for portability /
    manual import. Uses full hive names and CRLF, as Windows expects."""
    full = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE",
            "HKCR": "HKEY_CLASSES_ROOT", "HKU": "HKEY_USERS", "HKCC": "HKEY_CURRENT_CONFIG"}
    out = ["Windows Registry Editor Version 5.00", ""]
    for k in keys:
        section = full.get(k.root, k.root) + "\\" + k.path.replace("/", "\\")
        out.append(f"[{section}]")
        for v in k.values:
            out.append(v.to_wine())
        out.append("")
    return "\r\n".join(out)


def apply_to_prefix(keys: list[RegKey], pfx, backup: bool = True) -> list[str]:
    """Merge captured registry keys into a Proton/Wine prefix.

    Appends rendered sections to ``<pfx>/user.reg`` (HKCU) and
    ``<pfx>/system.reg`` (HKLM/HKCR). Wine applies the last definition of a key,
    so appending acts as an upsert. Returns the list of files modified.
    """
    import shutil
    from pathlib import Path

    pfx = Path(pfx)
    modified: list[str] = []
    for hive, filename in (("user", "user.reg"), ("system", "system.reg")):
        fragment = render_hive(keys, hive)
        if not fragment:
            continue
        target = pfx / filename
        if backup and target.exists():
            shutil.copy2(target, target.with_suffix(".reg.luduclone-bak"))
        header = "" if target.exists() else "WINE REGISTRY Version 2\n\n"
        with open(target, "a", encoding="utf-8") as f:
            if not header:
                f.write("\n")
            else:
                f.write(header)
            f.write(fragment)
        modified.append(str(target))
    return modified


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _hex_bytes(raw: bytes) -> str:
    return ",".join(f"{b:02x}" for b in raw)
