from __future__ import annotations

import numpy as np
from shapely.geometry import box

from segassign.zonal.counting import ClassEncoding, bincount_window, statistic_row
from segassign.zonal.shapefile import read_envelope_index, read_polygon_bounds, write_envelope_index
from segassign.zonal.spatial import CandidateIndex


def test_bincount_matches_reference_for_nodata_and_missing_classes():
    pixels = np.array([[0, 3, 3, 8], [4, 4, 99, 8]], dtype=np.int16)
    labels = np.array([[1, 1, 1, 2], [1, 2, 2, 2]], dtype=np.int32)
    candidates = np.array([7, 12], dtype=np.int64)
    encoding = ClassEncoding.create((3, 4, 8))

    actual = bincount_window(pixels, labels, candidates, encoding, nodata=0)

    assert actual == {7: {3: 2, 4: 1}, 12: {4: 1, 8: 2}}
    assert statistic_row(actual[7], 3, 65) == {
        "class": 3,
        "v1": 3,
        "v1p": 67,
        "v2": 4,
        "v2p": 33,
        "v3": 0,
        "v3p": 0,
    }


def test_strtree_candidates_equal_brute_force_envelope_filter():
    geometries = np.asarray([box(i, i, i + 2, i + 2) for i in range(50)], dtype=object)
    query_bounds = (9.5, 9.5, 14.25, 14.25)
    expected = np.flatnonzero(
        [
            geometry.bounds[0] <= query_bounds[2]
            and geometry.bounds[2] >= query_bounds[0]
            and geometry.bounds[1] <= query_bounds[3]
            and geometry.bounds[3] >= query_bounds[1]
            for geometry in geometries
        ]
    )

    actual = CandidateIndex(geometries).query(query_bounds)

    np.testing.assert_array_equal(actual, expected)


def test_shx_polygon_bounds_equal_decoded_geometry_bounds(tmp_path):
    import geopandas as gpd
    import pyogrio

    path = tmp_path / "bounds.shp"
    frame = gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[box(-3.5, 2.25, 8, 9.5), box(100, -20, 101, -10)],
        crs="EPSG:4326",
    )
    pyogrio.write_dataframe(frame, path, driver="ESRI Shapefile")
    np.testing.assert_allclose(read_polygon_bounds(path), np.asarray([item.bounds for item in frame.geometry]))


def test_envelope_index_rejects_changed_geometry_file(tmp_path):
    path = tmp_path / "bounds.shp"
    path.write_bytes(b"shape")
    bounds = np.asarray([[1.0, 2.0, 3.0, 4.0]])
    write_envelope_index(path, bounds)
    np.testing.assert_array_equal(read_envelope_index(path), bounds)
    path.write_bytes(b"changed-shape")
    assert read_envelope_index(path) is None
