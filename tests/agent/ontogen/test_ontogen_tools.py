"""Tool schemas, strict input validation, the untrusted envelope, sanitising and the proposal policy."""
import json

import pytest
from ontogen_helpers import bad_report, ok_report, profile_of, tid, widget_proposal

from agent.ontogen import tools as T
from agent.ontogen.generate import profile_hash_of
from engine.ingest.proposal import Proposal


def box(pd, validate_fn=lambda p, pr: ok_report()):
    return T.Toolbox(pd, validate_fn=validate_fn, proposal_cls=Proposal, namespace="https://isnad.local/x#",
                     profile_hash=profile_hash_of(pd))


@pytest.fixture(scope="module")
def inj():
    return profile_of("injection")


def test_exactly_four_tools_and_strict_schemas():
    sch = T.tool_schemas()
    assert [s["name"] for s in sch] == ["get_profile", "get_samples", "check_proposal", "submit_proposal"]
    assert all(s["input_schema"]["additionalProperties"] is False for s in sch)
    assert not any("sql" in json.dumps(s).lower() for s in sch)


@pytest.mark.parametrize("name,bad", [
    ("get_profile", {}), ("get_profile", {"table_id": "t1; DROP TABLE x"}), ("get_profile", {"table_id": "../etc/passwd"}),
    ("get_profile", {"table_id": 1}), ("get_profile", {"table_id": "t1", "path": "/etc"}),
    ("get_samples", {"column_id": "t1"}), ("get_samples", {"column_id": "t1.c1", "k": 0}),
    ("get_samples", {"column_id": "t1.c1", "k": 11}), ("get_samples", {"column_id": "t1.c1", "k": "5"}),
    ("get_samples", {"column_id": "t1.c1", "k": True}), ("check_proposal", {}), ("check_proposal", {"proposal": "x"}),
    ("submit_proposal", {"proposal": {}, "role": "admin"}), ("nope", {})])
def test_bad_inputs_are_errors_not_exceptions(inj, name, bad):
    out = box(inj).call(name, bad, "c1")
    assert not out.ok and not out.terminal and out.envelope["ok"] is False
    assert out.envelope["untrusted"] is True and out.envelope["tool_call_id"] == "c1"


def test_non_dict_input(inj):
    assert not box(inj).call("get_profile", "t1", "c").ok


def test_unknown_ids(inj):
    b = box(inj)
    assert b.call("get_profile", {"table_id": "t99"}, "c").error == "unknown table_id"
    assert b.call("get_samples", {"column_id": "t1.c99"}, "c").error == "unknown column_id"


def test_envelope_untrusted_and_injection_flags(inj):
    out = box(inj).call("get_profile", {"table_id": tid(inj, "notes")}, "c9")
    e = out.envelope
    assert e["ok"] and e["untrusted"] is True and e["tool_call_id"] == "c9"
    assert "ignore_instructions" in e["flags"] and "role_impersonation" in e["flags"]
    flagged = [c for c in e["data"]["columns"] if c.get("flags")]
    assert flagged and out.flags == e["flags"]
    plain = box(inj).call("get_profile", {"table_id": tid(inj, "widgets")}, "c")
    assert plain.envelope["flags"] == []


def test_truncation_and_control_chars(inj):
    out = box(inj).call("get_profile", {"table_id": tid(inj, "notes")}, "c")
    for c in out.envelope["data"]["columns"]:
        assert len(c["name"]) <= T.MAX_NAME
        for v, _ in c["top"]:
            assert len(v) <= T.MAX_VALUE
        assert all(len(x) <= T.MAX_VALUE for x in (c["min"] or "", c["max"] or ""))
    assert len(json.dumps(out.envelope)) < 20_000      # the 60k-character cell never reaches the model
    assert T.clean("a\x07b‮c\x00d") == "abcd"
    assert T.clean("x" * 200, 10) == "x" * 9 + "…"


def test_samples_capped_and_clean(inj):
    pd = json.loads(json.dumps(inj))
    col = pd["tables"][0]["columns"][0]
    col["top"] = [[f"v\x07{i}", 1] for i in range(20)]
    out = box(pd).call("get_samples", {"column_id": col["id"], "k": 10}, "c")
    assert out.ok and len(out.envelope["data"]["samples"]) == 10 and out.envelope["truncated"] is True
    assert "\x07" not in json.dumps(out.envelope)
    assert len(box(pd).call("get_samples", {"column_id": col["id"]}, "c").envelope["data"]["samples"]) == 5


def test_model_never_sees_paths_dsns_or_schema(inj):
    pd = json.loads(json.dumps(inj))
    pd["source"] = {"kind": "csv", "name": "/srv/secret/dir/data.csv", "sha256": "ab" * 32, "dsn": "postgresql://u:pw@h/db"}
    b = box(pd)
    blob = json.dumps([T.overview(pd), b.call("get_profile", {"table_id": "t1"}, "c").envelope])
    for needle in ("/srv/secret", "postgresql://", "pw@", "abab", "gen_", "ws_"):
        assert needle not in blob


def test_valid_proposal_checked_and_submitted(inj):
    p = widget_proposal(inj)
    rep = []
    b = box(inj, lambda pr, prop: (rep.append(prop), bad_report("evil\x07 example"))[1])
    chk = b.call("check_proposal", {"proposal": p}, "c")
    assert chk.ok and chk.report["ok"] is False
    ex = chk.report["checks"][0]["examples"]
    assert len(ex) == 3 and "\x07" not in json.dumps(ex) and len(chk.report["checks"][0]["detail"]) <= 160
    sub = box(inj).call("submit_proposal", {"proposal": p}, "c")
    assert sub.ok and sub.terminal and isinstance(sub.proposal, Proposal)
    assert sub.envelope["data"]["accepted"] is True


def test_model_cannot_choose_namespace_or_profile_hash(inj):
    p = widget_proposal(inj, namespace="https://evil.example/#", profile_hash="0" * 64)
    out = box(inj).call("submit_proposal", {"proposal": p}, "c")
    assert out.proposal.profile_hash == profile_hash_of(inj)
    assert out.proposal.namespace == "https://isnad.local/x#"


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["roles"].append({"name": "superuser", "description": "", "access": "admin"}), "admin"),
    (lambda p: p["roles"].append({"name": "root", "description": "", "access": "god"}), "access"),
    (lambda p: p["actions"].append({"name": "boom", "description": "", "kind": "run_function", "type": "Widget", "params": {}}), "kind"),
    (lambda p: p["actions"].append({"name": "hook", "description": "", "kind": "link", "type": "Nope", "params": {"link": "x"}}), "declared object type"),
    (lambda p: p.update(functions={"x": {}}), "unknown top-level"),
    (lambda p: p.update(webhooks={"x": {"url": "https://evil.example"}}), "unknown top-level"),
    (lambda p: p["objectTypes"][0]["properties"].append({"name": "x", "type": "string", "required": False, "column": "t9.c9",
                                                         "transform": "none", "label": ""}), "unknown column"),
])
def test_policy_rejects_injected_structures(inj, mutate, needle):
    p = widget_proposal(inj)
    mutate(p)
    out = box(inj).call("submit_proposal", {"proposal": p}, "c")
    assert not out.ok and not out.terminal and out.proposal is None
    assert needle in json.dumps(out.envelope["data"]["errors"])


def test_validation_errors_are_bounded(inj):
    """Errors may quote the model's own (short) value, but never grow with the input."""
    p = widget_proposal(inj)
    p["objectTypes"][0]["name"] = "x" * 5000
    p["rationale"] = "y" * 9000
    errs = box(inj).call("submit_proposal", {"proposal": p}, "c").envelope["data"]["errors"]
    assert 1 <= len(errs) <= T.MAX_ERRORS and all(len(e) <= 220 for e in errs)


def test_oversized_proposal_rejected(inj):
    p = widget_proposal(inj, rationale="x" * 300_000)
    assert "too large" in json.dumps(box(inj).call("check_proposal", {"proposal": p}, "c").envelope)


def test_validator_failure_is_generic(inj):
    def boom(*a):
        raise RuntimeError("postgresql://user:pw@host/db")
    out = box(inj, boom).call("check_proposal", {"proposal": widget_proposal(inj)}, "c")
    assert not out.ok and "pw@host" not in json.dumps(out.envelope) and "RuntimeError" in out.error
