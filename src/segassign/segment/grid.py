"""Deterministic 3072-core tile ownership grid with buffered read windows."""

from __future__ import annotations

from dataclasses import dataclass

from affine import Affine
from rasterio.windows import Window
from rasterio.windows import bounds as window_bounds


@dataclass(frozen=True, slots=True)
class Tile:
    tile_id: str
    row: int
    column: int
    core: Window
    buffer: Window
    core_bounds: tuple[float, float, float, float]
    buffer_bounds: tuple[float, float, float, float]

    def serializable(self) -> dict:
        def values(window: Window) -> tuple[int, int, int, int]:
            return tuple(int(value) for value in (window.col_off, window.row_off, window.width, window.height))

        return {
            "tile_id": self.tile_id,
            "row": self.row,
            "column": self.column,
            "core": values(self.core),
            "buffer": values(self.buffer),
            "core_bounds": self.core_bounds,
            "buffer_bounds": self.buffer_bounds,
        }


def expand(window: Window, pixels: int, width: int, height: int) -> Window:
    left = max(0, int(window.col_off) - pixels)
    top = max(0, int(window.row_off) - pixels)
    right = min(width, int(window.col_off + window.width) + pixels)
    bottom = min(height, int(window.row_off + window.height) + pixels)
    return Window(left, top, right - left, bottom - top)


def build_grid(width: int, height: int, transform: Affine, core_size: int, buffer_pixels: int) -> list[Tile]:
    result: list[Tile] = []
    for row_index, row in enumerate(range(0, height, core_size)):
        for column_index, column in enumerate(range(0, width, core_size)):
            core = Window(column, row, min(core_size, width - column), min(core_size, height - row))
            buffered = expand(core, buffer_pixels, width, height)
            result.append(
                Tile(
                    f"r{row_index:03d}_c{column_index:03d}",
                    row_index,
                    column_index,
                    core,
                    buffered,
                    tuple(float(value) for value in window_bounds(core, transform)),
                    tuple(float(value) for value in window_bounds(buffered, transform)),
                )
            )
    return result
