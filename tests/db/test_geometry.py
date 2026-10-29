from datetime import UTC, datetime, timedelta
from uuid import uuid4

from engine.db import bitemporal as bt

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P1 = {"type": "Point", "coordinates": [4.5, 51.9]}
P2 = {"type": "Point", "coordinates": [103.8, 1.3]}


def ship(ref="S", pos=P1, **kw):
    return {"ref": ref, "position": pos, **kw}


def outbox(conn):
    return conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]


def test_roundtrip_and_null(gconn):
    a, b = uuid4(), uuid4()
    bt.write_object(gconn, "Shipment", a, ship(), T0, "t")
    bt.write_object(gconn, "Shipment", b, {"ref": "N"}, T0, "t")
    assert bt.get_object(gconn, "Shipment", a, as_of_valid=T0)["position"] == P1
    assert bt.get_object(gconn, "Shipment", b, as_of_valid=T0)["position"] is None
    assert any(r["position"] == P1 for r in bt.list_objects(gconn, "Shipment", as_of_valid=T0))
    assert bt.history(gconn, "Shipment", a)[0]["position"] == P1


def test_noop_rewrite_identical_point(gconn):
    a = uuid4()
    assert bt.write_object(gconn, "Shipment", a, ship(), T0, "t")
    n = outbox(gconn)
    assert bt.write_object(gconn, "Shipment", a, ship(pos=dict(P1)), T0, "t") is False
    assert outbox(gconn) == n
    assert len(bt.history(gconn, "Shipment", a)) == 1
    # later identical write is also a no-op (merge of equal pieces)
    assert bt.write_object(gconn, "Shipment", a, ship(), T0 + timedelta(days=1), "t") is False


def test_changed_point_new_version_history_keeps_old(gconn):
    a = uuid4()
    bt.write_object(gconn, "Shipment", a, ship(), T0, "t")
    assert bt.write_object(gconn, "Shipment", a, ship(pos=P2), T0 + timedelta(days=1), "t")
    assert bt.get_object(gconn, "Shipment", a, as_of_valid=T0)["position"] == P1
    assert bt.get_object(gconn, "Shipment", a, as_of_valid=T0 + timedelta(days=2))["position"] == P2
    pts = [h["position"] for h in bt.history(gconn, "Shipment", a)]
    assert P1 in pts and P2 in pts


def test_range_split_preserves_geometry(gconn):
    a = uuid4()
    bt.write_object(gconn, "Shipment", a, ship(), T0, "t")
    # change a non-geo property in the middle: geometry must survive in all 3 pieces
    bt.write_object(gconn, "Shipment", a, ship(status="delayed"), T0 + timedelta(days=1), "t",
                    valid_to=T0 + timedelta(days=2))
    for d in (0, 1, 3):
        assert bt.get_object(gconn, "Shipment", a, as_of_valid=T0 + timedelta(days=d, hours=1))["position"] == P1
    raw = gconn.execute(
        "SELECT count(DISTINCT encode(ST_AsEWKB(\"position\"),'hex')) FROM obj_shipment WHERE id=%s",
        [a]).fetchone()[0]
    assert raw == 1


def test_clearing_geometry_is_a_change(gconn):
    a = uuid4()
    bt.write_object(gconn, "Shipment", a, ship(), T0, "t")
    assert bt.write_object(gconn, "Shipment", a, ship(pos=None), T0 + timedelta(hours=1), "t")
    assert bt.get_object(gconn, "Shipment", a, as_of_valid=T0 + timedelta(hours=2))["position"] is None


def test_bbox(gconn):
    a, b, c_ = uuid4(), uuid4(), uuid4()
    bt.write_object(gconn, "Shipment", a, ship("A", P1), T0, "t")
    bt.write_object(gconn, "Shipment", b, ship("B", P2), T0, "t")
    bt.write_object(gconn, "Shipment", c_, {"ref": "C"}, T0, "t")
    r = bt.list_objects_bbox(gconn, "Shipment", (0, 40, 10, 60), as_of_valid=T0)
    assert [x["ref"] for x in r] == ["A"]
    r = bt.list_objects_bbox(gconn, "Shipment", (100, 0, 110, 5), as_of_valid=T0, geom_col="position")
    assert [x["ref"] for x in r] == ["B"]
    assert bt.list_objects_bbox(gconn, "Shipment", (0, 40, 10, 60), as_of_valid=T0 - timedelta(days=1)) == []


def test_pagination_pushdown(gconn):
    ids = [uuid4() for _ in range(7)]
    for i, x in enumerate(ids):
        bt.write_object(gconn, "Shipment", x, {"ref": f"R{i}"}, T0, "t")
    allr = bt.list_objects(gconn, "Shipment", as_of_valid=T0, order_by="ref")
    assert [r["ref"] for r in allr] == [f"R{i}" for i in range(7)]
    page = bt.list_objects(gconn, "Shipment", as_of_valid=T0, order_by="ref", limit=3, offset=2)
    assert [r["ref"] for r in page] == ["R2", "R3", "R4"]
    desc = bt.list_objects(gconn, "Shipment", as_of_valid=T0, order_by="ref", desc=True, limit=2)
    assert [r["ref"] for r in desc] == ["R6", "R5"]
    assert len(bt.list_objects(gconn, "Shipment", as_of_valid=T0)) == 7  # old signature
    import pytest
    with pytest.raises(ValueError):
        bt.list_objects(gconn, "Shipment", order_by="nope; drop")


def test_bulk_load_with_geometry(gconn):
    rows = [(uuid4(), {"name": "P1", "location": P1, "status": "open"}),
            (uuid4(), {"name": "P2", "status": "open"})]
    assert bt.bulk_load_objects(gconn, "Port", rows, T0, "t") == 2
    got = {r["name"]: r["location"] for r in bt.list_objects(gconn, "Port", as_of_valid=T0)}
    assert got == {"P1": P1, "P2": None}
    # a later identical write is a no-op against bulk-loaded geometry
    assert bt.write_object(gconn, "Port", rows[0][0], rows[0][1], T0, "t") is False


def test_point_fast_path_matches_postgis(gconn):
    for g in (P1, P2, {"type": "Point", "coordinates": [-0.1, -33.25]}):
        db = gconn.execute(
            "SELECT encode(ST_AsEWKB(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),'hex')",
            [__import__("json").dumps(g)]).fetchone()[0]
        assert bt._geom_hex(gconn, g) == db


def test_nongeo_tables_unaffected(conn):
    a = uuid4()
    bt.write_object(conn, "Supplier", a, {"name": "x", "tier": 1}, T0, "t")
    assert bt.get_object(conn, "Supplier", a, as_of_valid=T0)["name"] == "x"
    assert bt.geometry_columns(conn, "Supplier") == ()
