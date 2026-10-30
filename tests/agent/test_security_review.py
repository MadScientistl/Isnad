"""Regressions for the Phase 4 security review (M3 ref forgery, M4 stale)."""
import pytest

from agent.tools import _REF_RE, _walk_refs
from engine.actions.core import _stale_keys

U = "22222222-2222-2222-2222-222222222222"
S = "11111111-1111-1111-1111-111111111111"


def test_property_named_ref_is_not_a_fact():
    out = []
    _walk_refs({"ref": f"Shipment:{S}", "props": {"ref": f"Invoice:{U}"}}, out)
    assert [f["ref"] for f in out] == [f"Shipment:{S}"]


@pytest.mark.parametrize("bad", [f"Invoice:{U}\n", f"Invoice:{U[:-1]}", "Invoice:" + "2222-" * 7 + "2",
                                 f"Invoice:{U.replace('-', '')}"])
def test_ref_regex_is_strict(bad):
    assert not _REF_RE.fullmatch(bad)


def test_link_added_since_proposal_is_stale():
    base = {"object:Shipment:s": {"status": "in_transit"}}
    diff = [{"kind": "object", "type": "Shipment", "key": "s", "before": {"status": "in_transit"}},
            {"kind": "link", "type": "ships_via", "key": "s>newport", "before": {"valid": True}}]
    assert _stale_keys(base, diff) == ["link:ships_via:s>newport"]


def test_new_object_in_diff_is_not_stale():
    base = {"object:Shipment:s": {"status": "x"}}
    diff = [{"kind": "object", "type": "Shipment", "key": "s", "before": {"status": "x"}},
            {"kind": "link", "type": "ships_via", "key": "s>p", "before": None}]
    assert _stale_keys(base, diff) == []


def test_no_event_after_finished_and_none_into_foreign_run(conn):
    import uuid
    import psycopg
    from engine.db import Principal, scoped
    rid = uuid.uuid4()
    me, other = Principal("planner", "u1", "", ()), Principal("admin", "boss", "", ())
    with conn.transaction(), scoped(conn, me):
        conn.execute('INSERT INTO agent_run (id, role, "user", prompt, model, caps) VALUES (%s,%s,%s,%s,%s,%s)',
                     [rid, "planner", "u1", "p", "m", "{}"])
        conn.execute("INSERT INTO agent_run_event (run_id, seq, kind, payload) VALUES (%s,1,'finished','{}')", [rid])
    with pytest.raises(psycopg.Error):
        with conn.transaction(), scoped(conn, me):
            conn.execute("INSERT INTO agent_run_event (run_id, seq, kind, payload) VALUES (%s,2,'finished','{}')", [rid])
    rid2 = uuid.uuid4()
    with conn.transaction(), scoped(conn, me):
        conn.execute('INSERT INTO agent_run (id, role, "user", prompt, model, caps) VALUES (%s,%s,%s,%s,%s,%s)',
                     [rid2, "planner", "u1", "p", "m", "{}"])
    with pytest.raises(psycopg.Error):   # admin can read every run but may not append to someone else's
        with conn.transaction(), scoped(conn, other):
            conn.execute("INSERT INTO agent_run_event (run_id, seq, kind, payload) VALUES (%s,1,'message','{}')", [rid2])
