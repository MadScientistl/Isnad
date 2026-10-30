"""Phase 5 security-review fixes for ontology versioning (L5): proposer != approver, YAML bounds, permission
widening needs a comment, one approved child per version."""
import copy

import psycopg
import pytest

from engine import versioning as V
from engine.compiler.model import OntologyError
from engine.db import Principal
from test_versioning import BASE_YAML, additive

ADMIN = Principal("admin", "adam")
ADMIN2 = Principal("admin", "ada")


@pytest.fixture(autouse=True)
def _no_self_approve(monkeypatch):
    monkeypatch.delenv("ISNAD_ONTOLOGY_SELF_APPROVE", raising=False)


def test_proposer_cannot_approve_their_own_proposal(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    p = V.propose(conn, ADMIN, additive(raw, dump), "x")
    with pytest.raises(V.VersioningError) as e:
        V.decide(conn, ADMIN, p["id"], True, "me")
    assert e.value.status == 403
    assert len(V.list_versions(conn, ADMIN)) == 1
    assert V.decide(conn, ADMIN2, p["id"], True, "four eyes")["version"]["source"] == "proposal"


def test_proposer_may_still_reject_and_dev_override_exists(conn, raw, dump, monkeypatch):
    V.register_current(conn, BASE_YAML)
    p = V.propose(conn, ADMIN, additive(raw, dump), "x")
    assert V.decide(conn, ADMIN, p["id"], False)["decision"] == "rejected"
    p2 = V.propose(conn, ADMIN, additive(raw, dump, "Other"), "x")
    monkeypatch.setenv("ISNAD_ONTOLOGY_SELF_APPROVE", "1")
    assert V.decide(conn, ADMIN, p2["id"], True)["decision"] == "approved"


def test_yaml_size_and_alias_bounds(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    with pytest.raises(OntologyError, match="too large"):
        V.diff(BASE_YAML, BASE_YAML + "\n# " + "x" * 200_001)
    bomb = "a: &a [1,2]\n" + "".join(f"b{i}: *a\n" for i in range(11))
    with pytest.raises(OntologyError, match="aliases"):
        V.diff(BASE_YAML, bomb)
    with pytest.raises(V.VersioningError) as e:
        V.propose(conn, ADMIN, bomb, "x")
    assert e.value.status == 422
    with pytest.raises(V.VersioningError):
        V.propose(conn, ADMIN, additive(raw, dump), "r" * 4001)


def test_permission_widening_is_sensitive_and_needs_a_comment(conn, raw, dump):
    V.register_current(conn, BASE_YAML)
    n = copy.deepcopy(raw)
    n["roles"]["regional_ops_lead"]["types"]["Supplier"]["mask"] = []           # un-mask a property
    n["roles"]["planner"]["actions"].append("pay_invoice")                      # a new action for an existing role
    n["actions"]["pay_invoice"]["roles"].append("planner")
    d = V.diff(BASE_YAML, dump(n))
    assert d["breaking"] == []
    assert any("no longer masked" in s for s in d["sensitive"])
    assert any("pay_invoice" in s for s in d["sensitive"])
    assert V.diff(BASE_YAML, additive(raw, dump))["sensitive"] == []           # a plain addition is not sensitive
    p = V.propose(conn, ADMIN, dump(n), "widen")
    assert p["diff"]["sensitive"]
    with pytest.raises(V.VersioningError) as e:
        V.decide(conn, ADMIN2, p["id"], True, "  ")
    assert e.value.status == 422 and "comment" in e.value.message
    assert V.decide(conn, ADMIN2, p["id"], True, "reviewed with security")["decision"] == "approved"


def test_one_approved_child_per_version(conn, raw, dump):
    v1 = V.register_current(conn, BASE_YAML)
    V.propose(conn, ADMIN, additive(raw, dump, "A1"), "")
    child = V._insert_version(conn, "y: 1", v1["hash"], "x", "proposal", None, v1["id"])
    assert child["parent_id"] == v1["id"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        V._insert_version(conn, "y: 2", v1["hash"], "x", "proposal", None, v1["id"])
    conn.rollback()
