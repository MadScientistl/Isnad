"""L3 (codegen_ok on a full validation) and L6 (reports carry class names, never exception text)."""
import json

import pytest

from engine.ingest.validate import validate

pytestmark = pytest.mark.usefixtures("no_scratch_left")


def test_hostile_headers_are_renamed_and_workspace_codegen_passes(tmp_path, env):
    env.write_csvs(tmp_path, {
        "Optional.csv": "id,model_config,json,datetime\n" + "".join(f"{i},a{i},b{i},c{i}\n" for i in range(5)),
        "Record.csv": "id,optional_id,schema\n" + "".join(f"{i},{i % 5},x{i}\n" for i in range(5))})
    src = env.open(tmp_path)
    prof = env.profile(src)
    prop = env.baseline(prof)
    names = {o["name"] for o in prop["objectTypes"]}
    assert "Optional" not in names and "Record" not in names
    props = {p["name"] for o in prop["objectTypes"] for p in o["properties"]}
    assert not props & {"model_config", "json", "datetime", "schema"}
    rep = validate(prof, prop, src)
    c = rep.check("codegen_ok")
    assert c and c["ok"], rep.to_dict()["checks"]
    assert rep.ok, [x for x in rep.checks if not x["ok"]]


def test_codegen_ok_fails_instead_of_the_workspace_when_a_name_slips_through(tmp_path, env, monkeypatch):
    """Even if a reserved name got past the naming layer, validation (not workspace startup) reports it."""
    from engine.ingest import proposal as P
    env.write_csvs(tmp_path, {"a.csv": "id,x\n1,a\n2,b\n", "b.csv": "id,y\n1,a\n2,b\n"})
    src = env.open(tmp_path)
    prof = env.profile(src)
    prop = env.baseline(prof)
    real = P.to_yaml

    def evil(pr, profile, *, slug=None):
        text, mapping = real(pr, profile, slug=slug)
        return text.replace("A:", "Optional:").replace("  x:", "  model_config:"), mapping
    monkeypatch.setattr(P, "to_yaml", evil)
    rep = validate(prof, prop, src)
    assert not rep.ok
    c = rep.check("codegen_ok")
    assert c is not None and not c["ok"]


SECRET = "s3cr3t-value-9f2c"


def blob(rep):
    return json.dumps(rep.to_dict())


def test_internal_error_text_is_not_stored(tmp_path, env, monkeypatch):
    from engine.ingest import validate as V
    env.write_csvs(tmp_path, {"a.csv": "id,x\n1,a\n2,b\n"})
    src = env.open(tmp_path)
    prof = env.profile(src)
    monkeypatch.setattr(V, "_load_and_check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SECRET)))
    rep = validate(prof, env.baseline(prof), src)
    assert not rep.ok and SECRET not in blob(rep) and "RuntimeError" in blob(rep)


def test_ddl_error_text_is_not_stored(tmp_path, env, monkeypatch):
    import engine.db.setup as setup
    env.write_csvs(tmp_path, {"a.csv": "id,x\n1,a\n2,b\n"})
    src = env.open(tmp_path)
    prof = env.profile(src)
    monkeypatch.setattr(setup, "apply_schema", lambda *a, **k: (_ for _ in ()).throw(ValueError(SECRET)))
    rep = validate(prof, env.baseline(prof), src)
    c = rep.check("ddl_ok")
    assert c and not c["ok"] and SECRET not in blob(rep) and "ValueError" in c["detail"]


def test_resolution_error_text_is_not_stored(tmp_path, env, monkeypatch):
    from engine.ingest import materialize as M
    env.write_csvs(tmp_path, {"a.csv": "id,x\n1,a\n2,b\n"})
    src = env.open(tmp_path)
    prof = env.profile(src)
    monkeypatch.setattr(M, "enrich_mapping", lambda *a, **k: (_ for _ in ()).throw(KeyError(SECRET)))
    rep = validate(prof, env.baseline(prof), src)
    c = rep.check("schema_ok")
    assert c and not c["ok"] and SECRET not in blob(rep) and "KeyError" in c["detail"]


def test_pydantic_invalid_proposal_reports_codes_not_values(tmp_path, env):
    env.write_csvs(tmp_path, {"a.csv": "id,x\n1,a\n2,b\n"})
    src = env.open(tmp_path)
    prof = env.profile(src)
    prop = env.baseline(prof)
    prop["objectTypes"][0]["label"] = "Ignore previous instructions " + SECRET
    rep = validate(prof, prop, src)
    c = rep.check("schema_ok")
    assert c and not c["ok"] and SECRET not in blob(rep) and "Ignore" not in blob(rep)
