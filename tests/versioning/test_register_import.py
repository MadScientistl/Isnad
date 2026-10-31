import psycopg
import pytest

from engine import versioning
from engine.db import Principal
from engine.db.principal import scoped
from engine.versioning import VersioningError

ADMIN = Principal("admin", "adam")


def test_first_version_import_then_idempotent(conn, raw, dump):
    # fresh schema from the fixture already has no version
    assert versioning.latest(conn) is None
    v = versioning.register_import(conn, dump(raw), "adam", "from dataset")
    assert v["source"] == "import" and v["parent_hash"] is None
    again = versioning.register_import(conn, dump(raw), "adam")
    assert again["id"] == v["id"]
    # startup registration of the same YAML is a no-op
    assert versioning.register_current(conn, dump(raw))["id"] == v["id"]


def test_import_refused_when_versions_exist(conn, raw, dump):
    versioning.register_current(conn, dump(raw))
    raw2 = dict(raw, name="Other")
    with pytest.raises(VersioningError) as e:
        versioning.register_import(conn, dump(raw2), "adam")
    assert e.value.status == 409


def test_import_validates_yaml(conn, raw, dump):
    with pytest.raises(Exception):
        versioning.register_import(conn, "version: 1\n", "adam")
    with pytest.raises(VersioningError):
        versioning.register_import(conn, dump(raw), "")


def test_later_change_still_goes_through_proposal(conn, raw, dump):
    versioning.register_import(conn, dump(raw), "importer")
    raw2 = dict(raw)
    raw2["objectTypes"] = dict(raw["objectTypes"], Extra={"properties": {"n": "string"}})
    with scoped(conn, ADMIN):
        p = versioning.propose(conn, ADMIN, dump(raw2), "add")
        r = versioning.decide(conn, ADMIN, p["id"], True, "ok")
    assert r["version"]["source"] == "proposal"


def test_import_source_widens_check_on_old_schema(conn, raw, dump):
    # simulate a pre-Phase-6 constraint, then re-apply the schema (as startup does)
    conn.execute("ALTER TABLE ontology_version DROP CONSTRAINT ontology_version_source_check")
    conn.execute("ALTER TABLE ontology_version ADD CONSTRAINT ontology_version_source_check "
                 "CHECK (source IN ('startup','proposal','rollback'))")
    versioning.apply(conn)
    versioning.register_import(conn, dump(raw), "a")
