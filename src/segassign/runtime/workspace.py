"""Fingerprinted checkpoints, reports, and recoverable atomic publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


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


class RunWorkspace:
    def __init__(self, root: Path, tool: str, fingerprint: str, resume: bool) -> None:
        self.root = Path(root).expanduser().resolve() / tool / fingerprint[:16]
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
            os.replace(source, _published_sidecar(source, staged, destination))
    except Exception:
        for published in shapefile_sidecars(destination):
            published.unlink(missing_ok=True)
        for old in backup.iterdir():
            old.replace(destination.parent / old.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
