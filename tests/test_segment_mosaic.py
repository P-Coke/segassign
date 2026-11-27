from __future__ import annotations

import geopandas as gpd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon, Polygon, box

from segassign.segment.mosaic import (
    _drop_subpixel_parts,
    _drop_subpixel_parts_from_multipolygons,
    _repair_roundtrip_topology,
    _subtract_main,
    assemble,
)
from segassign.segment.postprocess import _compute_seam_band, _intersecting_positions


def test_subtract_main_drops_only_subpixel_overlay_debris():
    main = gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs="EPSG:3857")
    supplements = gpd.GeoDataFrame(
        {"id": ["debris", "pixel"]},
        geometry=[box(0, 0, 1 + 1e-12, 1), box(1, 0, 2, 1)],
        crs=main.crs,
    )

    result = _subtract_main(supplements, main, minimum_area=1e-4)

    assert result["id"].tolist() == ["pixel"]
    assert result.geometry.iloc[0].area == 1.0


def test_subtract_main_normalizes_overlapping_supplement_parts():
    main = gpd.GeoDataFrame(geometry=[box(10, 10, 11, 11)], crs="EPSG:3857")
    overlapping = MultiPolygon([box(0, 0, 2, 2), box(1, 1, 3, 3)])
    supplements = gpd.GeoDataFrame({"id": [1]}, geometry=[overlapping], crs=main.crs)

    result = _subtract_main(supplements, main, minimum_area=1e-4)

    assert len(result) == 1
    assert result.geometry.is_valid.all()
    assert result.geometry.iloc[0].area == 7.0


def test_roundtrip_repair_changes_only_invalid_geometries():
    valid = box(10, 10, 11, 11)
    invalid = MultiPolygon([box(0, 0, 2, 2), box(1, 1, 3, 3)])
    frame = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[valid, invalid], crs="EPSG:3857")

    repaired, count = _repair_roundtrip_topology(frame)

    assert count == 1
    assert repaired.geometry.is_valid.all()
    assert shapely.equals_exact(repaired.geometry.iloc[0], valid, tolerance=0)
    assert repaired.geometry.iloc[1].area == 7.0


def test_subpixel_parts_are_removed_without_changing_main_polygon():
    main = box(0, 0, 2, 2)
    debris = box(10, 10, 10 + 1e-8, 10 + 1e-8)
    geometry = MultiPolygon([main, debris])

    repaired = _drop_subpixel_parts(geometry, minimum_area=1e-4)

    assert repaired.geom_type == "Polygon"
    assert shapely.equals_exact(repaired, main, tolerance=0)


def test_final_multipart_cleanup_keeps_real_parts_and_drops_numeric_debris():
    main = box(0, 0, 2, 2)
    real_part = box(3, 3, 3.1, 3.1)
    debris = box(10, 10, 10 + 1e-8, 10 + 1e-8)
    geometries = gpd.GeoSeries([main, MultiPolygon([main, real_part, debris])]).array

    cleaned = _drop_subpixel_parts_from_multipolygons(geometries, minimum_area=1e-4)

    assert shapely.equals_exact(cleaned[0], main, tolerance=0)
    assert len(shapely.get_parts(cleaned[1])) == 2
    assert shapely.equals(shapely.union_all(shapely.get_parts(cleaned[1])), shapely.union(main, real_part))


def test_final_multipart_cleanup_never_empties_an_entire_feature():
    tiny = MultiPolygon([box(0, 0, 1e-8, 1e-8), box(1, 1, 1 + 1e-8, 1 + 1e-8)])

    cleaned = _drop_subpixel_parts_from_multipolygons(gpd.GeoSeries([tiny]).array, minimum_area=1e-4)

    assert shapely.equals_exact(cleaned[0], tiny, tolerance=0)


def test_intersection_filter_preserves_spatial_index_traversal_order():
    geometries = gpd.GeoSeries([box(9, 9, 10, 10), box(0, 0, 1, 1), box(4, 4, 5, 5)]).array
    mask = box(-1, -1, 6, 6)
    expected = shapely.STRtree(geometries).query(mask)
    expected = expected[shapely.intersects(geometries[expected], mask)]

    positions = _intersecting_positions(geometries, mask)

    assert positions.tolist() == expected.tolist()


def test_assemble_orients_polygon_rings_for_shapefile_roundtrip(tmp_path):
    source = tmp_path / "tile.shp"
    destination = tmp_path / "result.shp"
    geometry = Polygon(
        [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)],
        [[(1, 1), (1, 3), (3, 3), (3, 1), (1, 1)]],
    )
    frame = gpd.GeoDataFrame(
        {
            "gap_fill": [0],
            "source_fid": [1],
            "tile_id": ["r000_c000"],
            "strategy": ["centroid_gapfill"],
        },
        geometry=[geometry],
        crs="EPSG:3857",
    )
    pyogrio.write_dataframe(frame, source, driver="ESRI Shapefile")

    assert assemble([source], destination, pixel_area=1.0) == 1
    written = pyogrio.read_dataframe(destination)
    assert not {"tile_id", "source_fid", "gap_fill"}.intersection(written.columns)
    assert written.geometry.is_valid.all()
    assert shapely.equals(written.geometry.iloc[0], geometry)


def test_assemble_multiple_tiles_keeps_attributes_and_hides_internal_fields(tmp_path):
    sources = []
    for position in range(2):
        source = tmp_path / f"tile-{position}.shp"
        frame = gpd.GeoDataFrame(
            {
                "name": [f"feature-{position}"],
                "gap_fill": [0],
                "source_fid": [position],
                "tile_id": [f"r000_c00{position}"],
            },
            geometry=[box(position, 0, position + 1, 1)],
            crs="EPSG:3857",
        )
        pyogrio.write_dataframe(frame, source, driver="ESRI Shapefile")
        sources.append(source)
    destination = tmp_path / "assembled.shp"

    assert assemble(sources, destination, pixel_area=1.0) == 2
    written = pyogrio.read_dataframe(destination)
    assert written["name"].tolist() == ["feature-0", "feature-1"]
    assert not {"tile_id", "source_fid", "gap_fill", "seam_band"}.intersection(written.columns)


def test_seam_band_tags_only_boundary_and_supplement_adjacent_main_polygons():
    core_bounds = (0.0, 0.0, 10.0, 10.0)
    interior = box(3, 3, 5, 5)
    touches_edge = box(0, 4, 2, 6)
    crosses_edge = box(9, 4, 11, 6)
    near_supp = box(5.5, 5.5, 7, 7)
    main_geoms = gpd.GeoSeries([interior, touches_edge, crosses_edge, near_supp]).array
    supp_geoms = gpd.GeoSeries([box(6.5, 6.5, 9.5, 9.5)]).array

    flags = _compute_seam_band(main_geoms, supp_geoms, core_bounds)

    assert flags.tolist() == [0, 1, 1, 1]


def test_assemble_reads_fgb_tile_caches_and_filters_seam_band(tmp_path):
    sources = []
    for position in range(2):
        source = tmp_path / f"tile-{position}.fgb"
        frame = gpd.GeoDataFrame(
            {
                "name": [f"main-{position}", f"supp-{position}"],
                "gap_fill": [0, 1],
                "seam_band": [1 if position == 0 else 0, 1],
                "source_fid": [position * 2, position * 2 + 1],
                "tile_id": [f"r000_c00{position}"] * 2,
            },
            geometry=[
                box(position * 2, 0, position * 2 + 2, 2),
                box(0, 0, 2, 2) if position == 1 else box(4, 0, 5, 1),
            ],
            crs="EPSG:3857",
        )
        pyogrio.write_dataframe(frame, source, driver="FlatGeobuf", spatial_index=False)
        sources.append(source)
    destination = tmp_path / "assembled_from_fgb.shp"

    count = assemble(sources, destination, pixel_area=1.0)
    assert count == 3  # supp-1 was fully covered by main-0 (seam_band=1) and removed
    written = pyogrio.read_dataframe(destination)
    assert not {"tile_id", "source_fid", "gap_fill", "seam_band"}.intersection(written.columns)
    assert written.geometry.is_valid.all()
