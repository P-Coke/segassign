"""Fingerprinted checkpoints, reports, lifecycle cache cleanup, and atomic publication."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

MANAGED_CACHE_TOOLS = frozenset({"segment", "segment-batch", "zonal", "zonal-batch", "composite", "composite-batch"})


def file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def dataset_fingerprint(path: Path) -> dict[str, Any]:
    if path.suffix.casefold() != ".shp":
        return file_fingerprint(path)
    sidecars = [
        item
        for item in shapefile_sidecars(path)
        if item.suffix.casefold() in {".shp", ".shx", ".dbf", ".prj", ".cpg"}
        or item.name.casefold().endswith(".gtz.npz")
    ]
    return {
        "path": str(path.resolve()),
        "sidecars": {
            ".gtz.npz" if item.name.casefold().endswith(".gtz.npz") else item.suffix.casefold(): file_fingerprint(item)
            for item in sidecars
        },
    }


def dataset_matches(path: Path, expected: dict[str, Any] | None) -> bool:
    if expected is None:
        return False
    try:
        return dataset_fingerprint(path) == expected
    except OSError:
        return False


def stable_digest(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _newest_mtime(path: Path) -> float:
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for item in path.rglob("*"):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            pass
    return newest


def _prune_empty_parents(start: Path, stop_at: Path) -> None:
    current = start
    stop_resolved = stop_at.resolve()
    while True:
        try:
            if not current.exists():
                current = current.parent
                continue
            current.rmdir()
        except OSError:
            break
        if current.resolve() == stop_resolved or current == current.parent:
            break
        current = current.parent


class RunWorkspace:
    def __init__(self, root: Path, tool: str, fingerprint: str, resume: bool) -> None:
        self.base_root = Path(root).expanduser().resolve()
        self.tool = tool
        self.fingerprint = fingerprint
        self.root = self.base_root / tool / fingerprint[:16]
        self.resume = resume
        self.checkpoints = self.root / "checkpoints"
        self.staging = self.root / "staging"
        self.reports = self.root / "reports"

    def ensure(self) -> None:
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)

    def checkpoint_path(self, key: str) -> Path:
        return self.checkpoints / f"{key}.json"

    def checkpoint_matches(self, key: str, fingerprint: str, required: tuple[Path, ...] = ()) -> bool:
        if not self.resume:
            return False
        try:
            payload = json.loads(self.checkpoint_path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("fingerprint") != fingerprint:
            return False
        if not all(path.is_file() and path.stat().st_size > 0 for path in required):
            return False
        for raw_path, expected in payload.get("artifacts", {}).items():
            path = Path(raw_path)
            try:
                if file_fingerprint(path) != expected:
                    return False
            except OSError:
                return False
        return True

    def read_checkpoint(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.checkpoint_path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def mark(self, key: str, fingerprint: str, **data: Any) -> None:
        atomic_json(self.checkpoint_path(key), {"fingerprint": fingerprint, **data})

    def report(self, name: str, payload: Any) -> Path:
        path = self.reports / f"{name}.json"
        atomic_json(path, payload)
        return path

    def cleanup_heavy(self, extra_dirs: tuple[Path, ...] = ()) -> tuple[str, ...]:
        """Remove heavy tile/sandbox/staging directories and per-tile checkpoints."""
        warnings: list[str] = []
        for directory in (*extra_dirs, self.staging):
            try:
                shutil.rmtree(directory, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                warnings.append(f"could not remove completed-run cache {directory}: {error}")
        if self.checkpoints.is_dir():
            for item in self.checkpoints.glob("*.json"):
                if item.name == "complete.json":
                    continue
                try:
                    item.unlink(missing_ok=True)
                except OSError as error:
                    warnings.append(f"could not remove tile checkpoint {item}: {error}")
        return tuple(warnings)

    def purge_after_success(self, report_path: Path) -> tuple[Path, tuple[str, ...]]:
        """Archive the lightweight run report and delete the entire run cache tree."""
        warnings: list[str] = []
        archived_report = report_path
        if report_path.is_file():
            archive_dir = self.base_root / "reports"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archived_report = archive_dir / f"{self.tool}-{self.fingerprint[:12]}-{report_path.name}"
            try:
                shutil.copy2(report_path, archived_report)
            except OSError as error:
                warnings.append(f"could not archive run report {report_path}: {error}")
                return report_path, tuple(warnings)
        try:
            shutil.rmtree(self.root, ignore_errors=False)
            _prune_empty_parents(self.root.parent, self.base_root)
        except FileNotFoundError:
            pass
        except OSError as error:
            warnings.append(f"could not remove completed-run workspace {self.root}: {error}")
        return archived_report, tuple(warnings)


def cache_usage_bytes(cache_root: Path) -> int:
    """Return total bytes occupied by managed SegAssign tool caches under cache_root."""
    root = Path(cache_root).expanduser().resolve()
    if not root.is_dir():
        return 0
    return sum(_dir_bytes(root / tool) for tool in MANAGED_CACHE_TOOLS if (root / tool).is_dir())


def prune_expired_cache(cache_root: Path, *, max_age_days: float = 7.0) -> tuple[int, int]:
    """Delete orphan run cache folders untouched for more than max_age_days."""
    root = Path(cache_root).expanduser().resolve()
    if not root.is_dir():
        return 0, 0
    cutoff = time.time() - max(0.0, max_age_days) * 86400.0
    removed = 0
    freed = 0
    for tool in MANAGED_CACHE_TOOLS:
        tool_dir = root / tool
        if not tool_dir.is_dir():
            continue
        for run_dir in list(tool_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            if _newest_mtime(run_dir) < cutoff:
                size = _dir_bytes(run_dir)
                try:
                    shutil.rmtree(run_dir)
                    removed += 1
                    freed += size
                except OSError:
                    pass
        _prune_empty_parents(tool_dir, root)
    return removed, freed


def clear_cache(cache_root: Path) -> tuple[int, int]:
    """Remove all managed SegAssign tool cache directories under cache_root."""
    root = Path(cache_root).expanduser().resolve()
    if not root.is_dir():
        return 0, 0
    removed = 0
    freed = 0
    for tool in MANAGED_CACHE_TOOLS:
        tool_dir = root / tool
        if not tool_dir.is_dir():
            continue
        size = _dir_bytes(tool_dir)
        try:
            shutil.rmtree(tool_dir)
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def shapefile_sidecars(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.stem + ".*"))


def _published_sidecar(source: Path, staged: Path, destination: Path) -> Path:
    suffix = source.name[len(staged.stem) :]
    return destination.with_name(destination.stem + suffix)


def publish_shapefile(staged: Path, destination: Path) -> None:
    required = tuple(staged.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf"))
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        raise RuntimeError(f"staged Shapefile is incomplete: {staged}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = stable_digest(str(destination))[:12]
    backup = destination.parent / f".backup-{token}"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir()
    previous = shapefile_sidecars(destination)
    try:
        for old in previous:
            old.replace(backup / old.name)
        for source in shapefile_sidecars(staged):
            target = _published_sidecar(source, staged, destination)
            try:
                os.replace(source, target)
            except OSError as error:
                # Cross-drive staging (e.g. NVMe D: cache -> USB G: output).
                if error.errno != errno.EXDEV and getattr(error, "winerror", None) != 17:
                    raise
                shutil.copy2(source, target)
                source.unlink(missing_ok=True)
    except Exception:
        for published in shapefile_sidecars(destination):
            published.unlink(missing_ok=True)
        for old in backup.iterdir():
            old.replace(destination.parent / old.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
