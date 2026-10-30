"""Value transforms of engine.ingest.materialize (pure, no database)."""
import datetime as dt

import pytest

from engine.ingest import materialize as M

UTC = dt.timezone.utc
ok = lambda v: not M.is_bad(v)  # noqa: E731


@pytest.mark.parametrize("s,fmt,expect", [
    ("2026-01-02", "iso8601", dt.datetime(2026, 1, 2, tzinfo=UTC)),
    ("2026-01-02T03:04:05Z", "iso8601", dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ("2026-01-02 03:04:05.123456789", "iso8601", dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)),
    ("2026-01-02T03:04:05+02:00", "iso8601", dt.datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC)),
    ("2026-01-02T03:04:05-0130", "iso8601", dt.datetime(2026, 1, 2, 4, 34, 5, tzinfo=UTC)),
    ("13/02/2020", "dd/mm/yyyy", dt.datetime(2020, 2, 13, tzinfo=UTC)),
    ("3.2.2020 14:05", "dd.mm.yyyy HH:MM[:SS]", dt.datetime(2020, 2, 3, 14, 5, tzinfo=UTC)),
    ("02/13/2020", "mm/dd/yyyy", dt.datetime(2020, 2, 13, tzinfo=UTC)),
    ("21 Jan 2026", "dd Mon yyyy", dt.datetime(2026, 1, 21, tzinfo=UTC)),
    ("2026-01-02 03:04", "yyyy-mm-dd HH:MM[:SS]", dt.datetime(2026, 1, 2, 3, 4, tzinfo=UTC)),
    ("20260102", "%Y%m%d", dt.datetime(2026, 1, 2, tzinfo=UTC)),
    ("1767225600", "epoch_s", dt.datetime(2026, 1, 1, tzinfo=UTC)),
    ("1767225600000", "epoch_ms", dt.datetime(2026, 1, 1, tzinfo=UTC)),
])
def test_timestamps(s, fmt, expect):
    assert M.parse_timestamp(s, fmt) == expect


@pytest.mark.parametrize("s,fmt", [
    ("02/13/2020", "dd/mm/yyyy"),         # ambiguous dates are never guessed: month 13 is a failure
    ("31/02/2020", "dd/mm/yyyy"),
    ("2026-13-01", "iso8601"), ("2026-02-30", "iso8601"), ("not a date", "iso8601"), ("2026-01-02T25:00", "iso8601"),
    ("1e30", "epoch_s"), ("-5", "epoch_s"), ("5/6/20", "dd/mm/yyyy"),
])
def test_timestamp_failures(s, fmt):
    assert M.is_bad(M.parse_timestamp(s, fmt))


def test_ambiguous_dates_follow_the_declared_order():
    a = M.parse_timestamp("03/04/2020", "dd/mm/yyyy")
    b = M.parse_timestamp("03/04/2020", "mm/dd/yyyy")
    assert (a.month, a.day) == (4, 3) and (b.month, b.day) == (3, 4)


@pytest.mark.parametrize("s,expect", [("12", 12), ("+12", 12), ("-7", -7), ("12.0", 12), ("1,234", 1234),
                                      ("1,234,567", 1234567), ("1e3", 1000)])
def test_int(s, expect):
    assert M.parse_int(s) == expect


@pytest.mark.parametrize("s", ["12.5", "1,23", "1_000", "abc", "", "nan", "inf", "9" * 25, "1,2345", "1.234,5"])
def test_int_failures(s):
    assert M.is_bad(M.parse_int(s))


@pytest.mark.parametrize("s,expect", [("1.5", 1.5), (".5", 0.5), ("1,234.5", 1234.5), ("1e-3", 0.001), ("-2", -2.0)])
def test_float(s, expect):
    assert M.parse_float(s) == expect


@pytest.mark.parametrize("s", ["nan", "inf", "-inf", "1e999", "1_0.5", "1,5", "x", "1.2.3"])
def test_float_failures(s):
    assert M.is_bad(M.parse_float(s))


@pytest.mark.parametrize("s,expect", [("yes", True), ("No", False), ("1", True), ("0", False), ("T", True), ("false", False),
                                      ("Y", True), ("n", False)])
def test_bool(s, expect):
    assert M.parse_bool(s) is expect


def test_bool_failures():
    assert all(M.is_bad(M.parse_bool(s)) for s in ("2", "maybe", "truee"))


def test_point_and_ranges():
    p = M.make_point("51.5", "-0.12")
    assert p == {"type": "Point", "coordinates": [-0.12, 51.5]}
    assert M.is_bad(M.make_point("91", "0")) and M.is_bad(M.make_point("0", "181")) and M.is_bad(M.make_point("a", "1"))


def test_geojson():
    assert M.parse_geojson('{"type":"Point","coordinates":[1,2]}', "point") == {"type": "Point", "coordinates": [1.0, 2.0]}
    f = '{"type":"Feature","geometry":{"type":"LineString","coordinates":[[0,0],[1,1]]}}'
    assert M.parse_geojson(f, "linestring")["type"] == "LineString"
    for bad, want in [('{"type":"Point","coordinates":[1,2]}', "linestring"), ('{"type":"Point","coordinates":[999,2]}', "point"),
                      ('{"type":"Point"', "point"), ("[1,2]", "point"), ('{"type":"LineString","coordinates":[[0,0]]}', "linestring"),
                      ('{"type":"Point","coordinates":[true,2]}', "point")]:
        assert M.is_bad(M.parse_geojson(bad, want))


def test_cell_normalisation():
    assert M.norm_cell("  hello\x00 world\x07 ") == "hello world"
    assert [M.norm_cell(x) for x in (None, "", "  ", "NA", "n/a", "NULL", "-", "nan")] == [None] * 8
    assert M.norm_cell("Nancy") == "Nancy"          # only whole-cell tokens are null


def test_object_id_is_deterministic_and_namespaced():
    a = M.object_id("https://x#", "T", ["1", "2"])
    assert a == M.object_id("https://x#", "T", ["1", "2"])
    assert a != M.object_id("https://x#", "T", ["12"]) and a != M.object_id("https://y#", "T", ["1", "2"])
    import uuid
    assert a == uuid.uuid5(uuid.NAMESPACE_URL, "https://x#T/1\x1f2")
