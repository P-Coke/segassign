"""Automatic discovery and path normalization for local eCognition SDK installations."""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Sequence
from pathlib import Path

ECOGNITION_SDK_FILES = ("Engine.dll", "DataIO.dll", "DBase.dll")

_COMMON_SUBDIRS = (
    "Program Files",
    "Program Files (x86)",
    "Program Files/Trimble",
    "Program Files (x86)/Trimble",
    "Trimble",
    "Definiens",
    "Software",
    "Tools",
    "GIS",
    "RS",
    "Apps",
)

_ENV_VARS = ("ECOGNITION_BIN", "ECOGNITION_HOME", "ECOGNITION_DIR")


def _has_sdk_files(directory: Path) -> bool:
    try:
        return directory.is_dir() and all((directory / name).is_file() for name in ECOGNITION_SDK_FILES)
    except OSError:
        return False


def resolve_ecognition_bin(path: str | os.PathLike[str]) -> Path | None:
    """Resolve an eCognition install root, bin folder, or executable to its validated SDK bin directory."""
    try:
        candidate = Path(path).expanduser()
        if candidate.is_file():
            candidate = candidate.parent
        candidate = candidate.resolve()
    except OSError:
        return None
    for probe in (candidate, candidate / "bin", candidate / "bin64"):
        if _has_sdk_files(probe):
            return probe
    return None


def normalize_ecognition_bin(path: str | os.PathLike[str]) -> Path:
    """Normalize a user-provided path to its SDK bin directory when detectable, else keep resolved path."""
    resolved = Path(path).expanduser().resolve()
    detected = resolve_ecognition_bin(resolved)
    return detected if detected is not None else resolved


def _local_drive_roots() -> list[Path]:
    if os.name != "nt":
        return [Path("/opt"), Path("/usr/local")]
    roots: list[Path] = []
    kernel32 = ctypes.windll.kernel32
    bitmask = kernel32.GetLogicalDrives()
    for index in range(26):
        if not (bitmask & (1 << index)):
            continue
        letter = chr(ord("A") + index)
        root_str = f"{letter}:\\"
        # 2 = DRIVE_REMOVABLE, 3 = DRIVE_FIXED; skip network/CDROM/RAM drives to avoid stalls.
        drive_type = kernel32.GetDriveTypeW(root_str)
        if drive_type in (2, 3):
            roots.append(Path(root_str))
    return roots


def _extract_path_from_command(raw: str) -> Path | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            text = text[1:end]
    else:
        # Handle unquoted "C:\Path\app.exe,0" or "C:\Path\uninst.exe /arg".
        text = text.split(",", 1)[0]
        match = re.match(r"^([A-Za-z]:\\.+?\.(?:exe|dll|msi))(?:\s|$)", text, flags=re.IGNORECASE)
        if match:
            text = match.group(1)
    try:
        return Path(text)
    except OSError:
        return None


def _registry_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    found: list[Path] = []
    uninstall_hives = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, subkey_path in uninstall_hives:
        try:
            with winreg.OpenKey(hive, subkey_path) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for index in range(count):
                    try:
                        child_name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, child_name) as child:
                            display_name = ""
                            try:
                                display_name = str(winreg.QueryValueEx(child, "DisplayName")[0])
                            except OSError:
                                pass
                            if (
                                "ecognition" not in display_name.casefold()
                                and "ecognition" not in child_name.casefold()
                            ):
                                continue
                            for value_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    raw = str(winreg.QueryValueEx(child, value_name)[0])
                                except OSError:
                                    continue
                                extracted = _extract_path_from_command(raw)
                                if extracted is not None:
                                    found.append(extracted)
                                    found.append(extracted.parent)
                    except OSError:
                        continue
        except OSError:
            continue

    vendor_hives = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Trimble"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Trimble"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Definiens"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Definiens"),
    )
    for hive, subkey_path in vendor_hives:
        try:
            with winreg.OpenKey(hive, subkey_path) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for index in range(count):
                    try:
                        child_name = winreg.EnumKey(parent, index)
                        if "ecognition" not in child_name.casefold():
                            continue
                        with winreg.OpenKey(parent, child_name) as child:
                            for value_name in ("InstallDir", "InstallLocation", "Path", "BinDir", "Home"):
                                try:
                                    raw = str(winreg.QueryValueEx(child, value_name)[0])
                                except OSError:
                                    continue
                                extracted = _extract_path_from_command(raw)
                                if extracted is not None:
                                    found.append(extracted)
                    except OSError:
                        continue
        except OSError:
            continue

    return found


def _env_candidates() -> list[Path]:
    found: list[Path] = []
    for var in _ENV_VARS:
        raw = os.environ.get(var, "").strip()
        if raw:
            found.append(Path(raw))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        lower = entry.casefold()
        if entry and any(token in lower for token in ("ecognition", "trimble", "definiens")):
            found.append(Path(entry))
    return found


def _scan_root_for_ecognition(root: Path) -> list[Path]:
    found: list[Path] = []
    search_bases = [root]
    for sub in _COMMON_SUBDIRS:
        candidate_base = root / sub
        if candidate_base.is_dir():
            search_bases.append(candidate_base)

    for base in search_bases:
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            name_low = entry.name.casefold()
            if "ecognition" in name_low:
                found.append(entry)
            elif name_low in ("trimble", "definiens"):
                try:
                    for nested in entry.iterdir():
                        if nested.is_dir() and "ecognition" in nested.name.casefold():
                            found.append(nested)
                except OSError:
                    continue
    return found


def _compatibility_rank(bin_path: Path) -> tuple[int, int, str]:
    text = str(bin_path).casefold()
    # Prefer exact eCognition Developer 9.0 (matched to the retained ecognition-9 bridge),
    # then any other 9.x installation, then other valid SDKs.
    if re.search(r"ecognition[^\\/]*9\.0(?:\b|[^0-9])", text):
        tier = 0
    elif re.search(r"ecognition[^\\/]*9(?:\.[0-9]+|\b)", text):
        tier = 1
    else:
        tier = 2
    return (tier, len(text), text)


def discover_ecognition_bins(
    *,
    extra_roots: Sequence[Path] = (),
    include_registry: bool = True,
    include_system_drives: bool = True,
) -> tuple[Path, ...]:
    """Discover all valid local eCognition SDK bin directories, ranked by compatibility."""
    raw_candidates: list[Path] = []
    raw_candidates.extend(_env_candidates())
    if include_registry:
        raw_candidates.extend(_registry_candidates())

    roots: list[Path] = [Path(root) for root in extra_roots]
    if include_system_drives:
        roots.extend(_local_drive_roots())

    for root in roots:
        raw_candidates.append(root)
        raw_candidates.extend(_scan_root_for_ecognition(root))

    unique: dict[str, Path] = {}
    for candidate in raw_candidates:
        resolved = resolve_ecognition_bin(candidate)
        if resolved is None:
            continue
        key = str(resolved).casefold()
        unique.setdefault(key, resolved)

    return tuple(sorted(unique.values(), key=_compatibility_rank))


def detect_ecognition_bin(
    *,
    extra_roots: Sequence[Path] = (),
    include_registry: bool = True,
    include_system_drives: bool = True,
) -> Path | None:
    """Return the highest-priority valid local eCognition SDK bin directory, or None."""
    found = discover_ecognition_bins(
        extra_roots=extra_roots,
        include_registry=include_registry,
        include_system_drives=include_system_drives,
    )
    return found[0] if found else None
