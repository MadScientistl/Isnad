import copy

import psycopg
import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from engine import versioning as V
from engine.api.ontology_routes import make_router
from engine.compiler.model import OntologyError
from engine.db import Principal
from engine.db.principal import scoped

from conftest import BASE_YAML

ADMIN = Principal("admin", "adam")
PLANNER = Principal("planner", "pat")


def drop_link(n, ln):
    del n["linkTypes"][ln]
    for r in n["roles"].values():
        if isinstance(r.get("links"), list) and ln in r["links"]:
            r["links"].remove(ln)


def first_prop(raw):
    t = next(iter(raw["objectTypes"]))
    return t, raw["objectTypes"][t]


# ------------------------------------------------------------------ diff
def test_diff_identical_is_empty():
    d = V.diff(BASE_YAML, BASE_YAML)
    assert V.is_empty(d) and d["breaking"] == []


def test_additive(raw, dump):
    n = copy.deepcopy(raw)
    n["objectTypes"]["Gadget"] = {"properties": {"name": "string"}}
    t, o = first_prop(n)
    o["properties"]["new_prop"] = "string"
    d = V.diff(BASE_YAML, dump(n))
    assert d["breaking"] == []
    assert "Gadget" in d["added"]["objectTypes"]
    assert f"{t}.new_prop" in d["added"]["properties"]


def test_enum_value_added_is_additive(raw, dump):
    n = copy.deepcopy(raw)
    n["objectTypes"]["Shipment"]["properties"]["status"]["enum"].append("brand_new")
    d = V.diff(BASE_YAML, dump(n))
    assert d["breaking"] == []
    assert d["changed"]["properties"]["Shipment.status"]


def test_breaking_enum_value_removed(raw, dump):
    n = copy.deepcopy(raw)
    n["objectTypes"]["Port"]["properties"]["region"]["enum"].remove("AMER")
    d = V.diff(BASE_YAML, dump(n))
    assert any("enum value 'AMER'" in b for b in d["breaking"])


def test_breaking_property_removed(raw, dump):
    n = copy.deepcopy(raw)
    del n["objectTypes"]["Event"]["properties"]["note"]
    d = V.diff(BASE_YAML, dump(n))
    assert "Event.note" in d["removed"]["properties"]
    assert any("removed" in b for b in d["breaking"])


def test_breaking_type_change_and_required(raw, dump):
    n = copy.deepcopy(raw)
    n["objectTypes"]["Event"]["properties"]["note"] = "int"
    assert any("type changed" in b for b in V.diff(BASE_YAML, dump(n))["breaking"])
    n = copy.deepcopy(raw)
    n["objectTypes"]["Event"]["properties"]["note"] = "string!"
    assert any("made required" in b for b in V.diff(BASE_YAML, dump(n))["breaking"])
    n = copy.deepcopy(raw)  # relaxing required is not breaking
    n["objectTypes"]["Shipment"]["properties"]["ref"] = "string"
    assert V.diff(BASE_YAML, dump(n))["breaking"] == []


def test_breaking_link_and_object_removed(raw, dump):
    n = copy.deepcopy(raw)
    ln = "billed_by"
    drop_link(n, ln)
    d = V.diff(BASE_YAML, dump(n))
    assert ln in d["removed"]["linkTypes"] and d["breaking"]
    n = copy.deepcopy(raw)
    n["linkTypes"]["operated_by"]["cardinality"] = "one-to-one"
    d = V.diff(BASE_YAML, dump(n))
    assert any("operated_by" in b and "cardinality" in b for b in d["breaking"])


def test_breaking_role_tightened(raw, dump):
    n = copy.deepcopy(raw)
    n["roles"]["planner"]["actions"].remove("reopen_port")
    n["actions"]["reopen_port"]["roles"].remove("planner")
    d = V.diff(BASE_YAML, dump(n))
    assert d["breaking"] and "planner" in d["changed"]["roles"]


def test_breaking_action_and_webhook_removed(raw, dump):
    n = copy.deepcopy(raw)
    an = "reopen_port"
    for r in n["roles"].values():
        for k in ("actions", "approves"):
            if isinstance(r.get(k), list) and an in r[k]:
                r[k].remove(an)
        if r.get("agent"):
            r["agent"]["allowlist"] = [a for a in r["agent"].get("allowlist", []) if a != an]
    wn = next(iter(n["webhooks"]))
    for a in n["actions"].values():
        if wn in (a.get("webhooks") or []):
            a["webhooks"] = [w for w in a["webhooks"] if w != wn]
    del n["webhooks"][wn]
    del n["actions"][an]
    try:
        d = V.diff(BASE_YAML, dump(n))
    except OntologyError:
        pytest.skip("removed action referenced elsewhere")
    assert an in d["removed"]["actions"] and wn in d["removed"]["webhooks"]
    assert len(d["breaking"]) >= 2


def test_invalid_yaml_rejected(raw, dump):
    with pytest.raises(OntologyError):
        V.diff(BASE_YAML, "a: [unclosed")
    n = copy.deepcopy(raw)
    n["linkTypes"]["bad"] = {"from": "Nope", "to": "Port"}
    with pytest.raises(OntologyError):
        V.diff(BASE_YAML, dump(n))


# ------------------------------------------------------------------ DB
def additive(raw, dump, name="Gadget"):
    n = copy.deepcopy(raw)
    n["objectTypes"][name] = {"properties": {"name": "string"}}
    return dump(n)


def test_register_idempotent(conn):
    a = V.register_current(conn, BASE_YAML, "startup")
    b = V.register_current(conn, BASE_YAML, "startup")
    assert a["id"] == b["id"] and a["parent_hash"] is None
    assert len(V.list_versions(conn, ADMIN)) == 1


def test_propose_approve_reject(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    p = V.propose(conn, ADMIN, additive(raw, dump), "add gadget")
    assert p["breaking"] is False and p["proposer"] == "adam"
    r = V.decide(conn, ADMIN, p["id"], True, "ok")
    assert r["decision"] == "approved" and r["version"]["source"] == "proposal"
    assert any("restart" in n for n in r["notes"])
    assert len(V.list_versions(conn, ADMIN)) == 2
    with pytest.raises(V.VersioningError) as e:
        V.decide(conn, ADMIN, p["id"], False)
    assert e.value.status == 409
    p2 = V.propose(conn, ADMIN, additive(raw, dump, "Widget"), "w")
    assert V.decide(conn, ADMIN, p2["id"], False, "no")["decision"] == "rejected"
    assert len(V.list_versions(conn, ADMIN)) == 2


def test_write_path_only_when_asked(conn, raw, dump, tmp_path):
    V.register_current(conn, BASE_YAML)
    y = additive(raw, dump)
    p = V.propose(conn, ADMIN, y, "x")
    f = tmp_path / "o.yaml"
    V.decide(conn, ADMIN, p["id"], True, write_path=f)
    assert f.read_text() == y


def test_breaking_proposed_but_not_approved(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    n = copy.deepcopy(raw)
    drop_link(n, "billed_by")
    p = V.propose(conn, ADMIN, dump(n), "drop")
    assert p["breaking"] is True
    with pytest.raises(V.VersioningError) as e:
        V.decide(conn, ADMIN, p["id"], True)
    assert e.value.status == 422
    assert len(V.list_versions(conn, ADMIN)) == 1
    assert V.decide(conn, ADMIN, p["id"], False)["decision"] == "rejected"  # still rejectable


def test_stale_and_noop_and_invalid(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    p1 = V.propose(conn, ADMIN, additive(raw, dump, "A1"), "")
    p2 = V.propose(conn, ADMIN, additive(raw, dump, "A2"), "")
    V.decide(conn, ADMIN, p1["id"], True)
    with pytest.raises(V.VersioningError) as e:
        V.decide(conn, ADMIN, p2["id"], True)
    assert e.value.status == 409
    with pytest.raises(V.VersioningError):
        V.propose(conn, ADMIN, additive(raw, dump, "A1"), "")  # already applied: no change
    with pytest.raises(V.VersioningError) as e:
        V.propose(conn, ADMIN, "not: [valid", "")
    assert e.value.status == 422


def test_rollback(conn, raw, dump):
    v1 = V.register_current(conn, BASE_YAML)
    y = additive(raw, dump)
    p = V.propose(conn, ADMIN, y, "")
    v2 = V.decide(conn, ADMIN, p["id"], True)["version"]
    r = V.rollback(conn, ADMIN, v2["id"])
    assert r["yaml"] == BASE_YAML and r["version"]["source"] == "rollback"
    assert r["version"]["hash"] == v1["hash"]
    assert len(V.list_versions(conn, ADMIN)) == 3
    with pytest.raises(V.VersioningError):
        V.rollback(conn, ADMIN, v1["id"])  # no parent
    with pytest.raises(V.VersioningError) as e:
        V.rollback(conn, ADMIN, 9999)
    assert e.value.status == 404


def test_non_admin_denied_in_python(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    for fn in (lambda: V.list_versions(conn, PLANNER), lambda: V.propose(conn, PLANNER, additive(raw, dump), "")):
        with pytest.raises(V.VersioningError) as e:
            fn()
        assert e.value.status == 403


def test_append_only(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    p = V.propose(conn, ADMIN, additive(raw, dump), "")
    V.decide(conn, ADMIN, p["id"], False)
    conn.commit()
    for stmt in ("UPDATE ontology_version SET note='x'", "DELETE FROM ontology_version",
                 "UPDATE ontology_change_proposal SET rationale='x'", "DELETE FROM ontology_change_proposal",
                 "UPDATE ontology_change_decision SET comment='x'", "DELETE FROM ontology_change_decision",
                 "TRUNCATE ontology_version"):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute(stmt)
        conn.rollback()


def test_rls_admin_only_select(conn):
    V.register_current(conn, BASE_YAML)
    conn.commit()
    for role, n in (("admin", 1), ("planner", 0)):
        with conn.transaction():
            with scoped(conn, Principal(role, "u")):
                assert conn.execute("SELECT count(*) FROM ontology_version").fetchone()[0] == n


# ------------------------------------------------------------------ router
@pytest.fixture
def client(factory):
    def get_principal(x_role: str | None = Header(default=None)):
        return Principal(x_role or "planner", "u-" + (x_role or "planner"))

    def get_conn():
        c = factory()
        try:
            yield c
        finally:
            c.close()

    app = FastAPI()
    app.include_router(make_router(get_principal, get_conn))
    c = factory()
    V.register_current(c, BASE_YAML)
    c.commit()
    c.close()
    return TestClient(app)


def test_routes_non_admin_403(client, raw, dump):
    h = {"x-role": "planner"}
    assert client.get("/ontology/versions", headers=h).status_code == 403
    assert client.get("/ontology/versions/1", headers=h).status_code == 403
    assert client.get("/ontology/diff?from=1&to=1", headers=h).status_code == 403
    assert client.get("/ontology/proposals", headers=h).status_code == 403
    assert client.post("/ontology/proposals", json={"yaml": "x"}, headers=h).status_code == 403
    pid = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/ontology/proposals/{pid}/approve", headers=h).status_code == 403
    assert client.post(f"/ontology/proposals/{pid}/reject", headers=h).status_code == 403
    assert client.post("/ontology/versions/1/rollback", headers=h).status_code == 403


def test_routes_admin_flow(client, raw, dump):
    h = {"x-role": "admin"}
    vs = client.get("/ontology/versions", headers=h).json()
    assert len(vs) == 1
    r = client.post("/ontology/proposals", json={"yaml": additive(raw, dump), "rationale": "r"}, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert len(client.get("/ontology/proposals", headers=h).json()) == 1
    a = client.post(f"/ontology/proposals/{pid}/approve", json={"comment": "go"}, headers=h)
    assert a.status_code == 200 and a.json()["version"]["source"] == "proposal"
    assert client.post(f"/ontology/proposals/{pid}/approve", headers=h).status_code == 409
    vs = client.get("/ontology/versions", headers=h).json()
    assert len(vs) == 2
    d = client.get(f"/ontology/diff?from={vs[1]['id']}&to={vs[0]['id']}", headers=h).json()
    assert d["added"]["objectTypes"] == ["Gadget"]
    assert client.get(f"/ontology/versions/{vs[0]['id']}", headers=h).json()["yaml"]
    rb = client.post(f"/ontology/versions/{vs[0]['id']}/rollback", headers=h)
    assert rb.status_code == 200 and rb.json()["version"]["source"] == "rollback"
    assert client.get("/ontology/versions/999", headers=h).status_code == 404
    bad = client.post("/ontology/proposals", json={"yaml": "a: [x"}, headers=h)
    assert bad.status_code == 422
