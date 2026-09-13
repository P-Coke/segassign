"""Bounded raster read recovery with source and window diagnostics."""

from __future__ import annotations

import rasterio
from rasterio.errors import RasterioIOError
from rasterio.windows import Window

from ..runtime import RunContext


class RasterReadError(RuntimeError):
    def __init__(self, diagnostic: dict) -> None:
        self.diagnostic = diagnostic
        super().__init__(
            f"影像读取失败（{diagnostic['role']}）：{diagnostic['source']}；"
            f"tile={','.join(diagnostic['tile_ids'])}；window={diagnostic['window']}；"
            f"已尝试 {diagnostic['attempts']} 次。请检查文件完整性及存储连接；"
            f"未将读取失败区域作为无效像元跳过。底层错误：{diagnostic['errors'][-1]}"
        )


def _error_chain(error: BaseException) -> str:
    messages = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        messages.append(f"{type(error).__name__}: {error}")
        error = error.__cause__ or error.__context__
    return " <- ".join(messages)


class RasterWindowReader:
    """Keep the normal handle until a read fails; replace it for all later reads.

    A failed GDAL handle may retain partially populated cached blocks. Never
    reuse that handle, including for subsequent, overlapping tile windows.
    """

    def __init__(self, dataset, *, role: str, context: RunContext | None = None, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.source = dataset
        self.dataset = dataset
        self.role = role
        self.context = context
        self.max_attempts = max_attempts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._discard()

    def _discard(self):
        if self.dataset is not None and self.dataset is not self.source:
            self.dataset.close()
        self.dataset = None

    def read(self, window: Window, *, tile_ids: tuple[str, ...]):
        diagnostic = {
            "source": str(self.source.name),
            "role": self.role,
            "tile_ids": list(tile_ids),
            "window": dict(zip(("col_off", "row_off", "width", "height"), window.flatten(), strict=True)),
            "raster_shape": [self.source.count, self.source.height, self.source.width],
            "block_shapes": [list(shape) for shape in self.source.block_shapes],
            "attempts": 0,
            "errors": [],
        }
        for attempt in range(1, self.max_attempts + 1):
            if self.context is not None:
                self.context.checkpoint()
            try:
                if self.dataset is None:
                    self.dataset = rasterio.open(self.source.name, sharing=False, NUM_THREADS="1")
                data = self.dataset.read(window=window, masked=True)
                if attempt > 1 and self.context is not None:
                    self.context.emit(
                        "warning", "prepare", f"重新打开后读取成功：{self.source.name}（第 {attempt} 次尝试）",
                        source=str(self.source.name), tile_ids=tile_ids, attempts=attempt,
                    )
                return data
            except RasterioIOError as error:
                self._discard()
                diagnostic["attempts"] = attempt
                diagnostic["errors"].append(_error_chain(error))
                if attempt == self.max_attempts:
                    raise RasterReadError(diagnostic) from error
                if self.context is not None:
                    self.context.emit(
                        "warning", "prepare",
                        f"影像读取失败，正在重新打开并重试（{attempt + 1}/{self.max_attempts}）：{self.source.name}",
                        source=str(self.source.name), role=self.role, tile_ids=tile_ids,
                        window=diagnostic["window"], error=diagnostic["errors"][-1],
                    )
