"""Private adapter for the retained bridge and user-installed eCognition SDK."""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil
import pyogrio

from ..models import SegmentRequest
from ..runtime import CancelledError, RunContext
from .ruleset import retarget

RUNNER_NAME = "runecognitionami-ecognition_developer_9.0.exe"
_DLL_DIRECTORY_LOCK = threading.Lock()


@contextlib.contextmanager
def _external_dll_search_path():
    """Keep PyInstaller's private DLL directory out of eCognition children."""
    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel32.GetDllDirectoryW
    get_directory.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p]
    get_directory.restype = ctypes.c_ulong
    set_directory = kernel32.SetDllDirectoryW
    set_directory.argtypes = [ctypes.c_wchar_p]
    set_directory.restype = ctypes.c_bool
    with _DLL_DIRECTORY_LOCK:
        size = get_directory(0, None)
        buffer = ctypes.create_unicode_buffer(size + 1)
        if size:
            get_directory(len(buffer), buffer)
        previous = buffer.value or None
        if not set_directory(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not set_directory(previous):
                raise ctypes.WinError(ctypes.get_last_error())


def external_engine_processes() -> list[int]:
    """Return runner PIDs not descended from this backend process."""
    owner = os.getpid()
    result: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.name().casefold() != RUNNER_NAME:
                continue
            if any(parent.pid == owner for parent in process.parents()):
                continue
            result.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def wait_for_external_engines(context: RunContext, poll_seconds: float = 1.0) -> None:
    reported: tuple[int, ...] = ()
    idle_since: float | None = None
    while True:
        context.checkpoint()
        current = tuple(external_engine_processes())
        now = time.monotonic()
        if current:
            idle_since = None
            if current != reported:
                context.emit(
                    "waiting",
                    "ecognition",
                    "Waiting for external eCognition runners to become idle",
                    process_ids=current,
                )
                reported = current
        else:
            if not reported:
                return
            idle_since = idle_since or now
            if now - idle_since >= 2.0:
                return
        time.sleep(poll_seconds)


def _link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _complete_vector(path: Path) -> bool:
    try:
        required = [path.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf")]
        return (
            all(item.is_file() and item.stat().st_size > 0 for item in required)
            and int(pyogrio.read_info(path)["features"]) >= 0
        )
    except Exception:
        return False


def _sandbox(tile_dir: Path, root: Path, attempt: int) -> dict[str, Path]:
    sandbox = root / tile_dir.name / f"a{attempt}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    paths = {
        "root": sandbox,
        "process": sandbox / "p.tif",
        "base": sandbox / "b.tif",
        "ruleset": sandbox / "r.dcp",
        "project": sandbox / "x",
        "export": sandbox / "o",
        "temp": sandbox / "t",
    }
    for name in ("project", "export", "temp"):
        paths[name].mkdir(parents=True, exist_ok=True)
    _link(tile_dir / "input" / "process_tile.tif", paths["process"])
    _link(tile_dir / "input" / "base_tile.tif", paths["base"])
    retarget(tile_dir / "ruleset" / "process_tile.dcp", paths["ruleset"], paths["export"])
    if max(len(str(path)) for path in paths.values()) >= 180:
        raise RuntimeError(f"eCognition sandbox path exceeds the safe Windows limit: {sandbox}")
    return paths


def _terminate(process: subprocess.Popen) -> None:
    try:
        root = psutil.Process(process.pid)
        targets = [*root.children(recursive=True), root]
    except psutil.Error:
        targets = []
    for target in reversed(targets):
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass


def _invoke(command: list[str], cwd: Path, environment: dict[str, str], timeout: float, context: RunContext):
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with _external_dll_search_path():
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    deadline = time.monotonic() + timeout
    while True:
        if context.cancellation.cancelled:
            _terminate(process)
            process.communicate()
            raise CancelledError("task cancelled during eCognition execution")
        if time.monotonic() >= deadline:
            _terminate(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, stdout, stderr)
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            return int(process.returncode), stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            continue


def _find_export(paths: dict[str, Path]) -> Path | None:
    for candidate in (paths["root"] / "o.shp", paths["export"] / "ObjectShapes.shp"):
        if _complete_vector(candidate):
            return candidate
    return None


def _publish_export(source: Path, tile_dir: Path) -> Path:
    directory = tile_dir / "ecognition_export"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    destination = directory / "ObjectShapes.shp"
    for sidecar in source.parent.glob(source.stem + ".*"):
        target = destination.with_suffix(sidecar.suffix)
        try:
            os.replace(sidecar, target)
        except OSError:
            shutil.copy2(sidecar, target)
    if not _complete_vector(destination):
        raise RuntimeError(f"eCognition export is incomplete after publication: {destination}")
    return destination


def run_tile(tile_dir: Path, sandbox_root: Path, request: SegmentRequest, context: RunContext) -> dict:
    started = time.perf_counter()
    attempts: list[dict] = []
    environment = os.environ.copy()
    environment["PATH"] = str(request.ecognition_bin) + os.pathsep + environment.get("PATH", "")
    for attempt in range(1, request.options.max_attempts + 1):
        context.checkpoint()
        paths = _sandbox(tile_dir, sandbox_root, attempt)
        command = [
            str(request.bridge),
            "--image",
            str(paths["process"]),
            "--image",
            str(paths["base"]),
            "--ruleset",
            str(paths["ruleset"]),
            "--out-dir",
            str(paths["project"]),
            "--project-name",
            tile_dir.name,
            "--temp-dir",
            str(paths["temp"]),
        ]
        attempt_started = time.perf_counter()
        try:
            return_code, stdout, stderr = _invoke(
                command, request.ecognition_bin, environment, request.options.timeout_seconds, context
            )
            export = _find_export(paths)
            error = ""
            if return_code == 0 and export is not None:
                destination = _publish_export(export, tile_dir)
                shutil.rmtree(paths["root"], ignore_errors=True)
                attempts.append(
                    {"attempt": attempt, "returncode": return_code, "seconds": time.perf_counter() - attempt_started}
                )
                return {
                    "tile_id": tile_dir.name,
                    "export": str(destination),
                    "attempts": attempts,
                    "seconds": time.perf_counter() - started,
                }
            error = (
                "bridge returned success without a complete ObjectShapes Shapefile"
                if return_code == 0
                else f"bridge exited with code {return_code}"
            )
        except CancelledError:
            raise
        except Exception as exception:
            stdout = ""
            stderr = repr(exception)
            return_code = -1
            error = f"{type(exception).__name__}: {exception}"
        (tile_dir / f"engine-attempt-{attempt:02d}.stdout.txt").write_text(stdout, encoding="utf-8", errors="ignore")
        (tile_dir / f"engine-attempt-{attempt:02d}.stderr.txt").write_text(stderr, encoding="utf-8", errors="ignore")
        attempts.append(
            {
                "attempt": attempt,
                "returncode": return_code,
                "error": error,
                "seconds": time.perf_counter() - attempt_started,
            }
        )
        if attempt < request.options.max_attempts:
            time.sleep(min(10.0, attempt * 2.0))
    raise RuntimeError(f"eCognition failed for {tile_dir.name}: {attempts[-1]['error']}")
