"""Adversarial security suite fixtures: a scratch schema with RLS applied and a crafted world.

Roles under test: regional_ops_lead (EMEA), planner, finance_analyst, admin plus `restricted`, a
test-only role added to a COPY of the ontology (Supplier rows filtered by country) so that
`supplies` chains can have hidden middle nodes.
"""
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.compiler import compile_ontology  # noqa: E402
from engine.db import bitemporal as bt, connect, get_dsn  # noqa: E402
from engine.db.principal import Principal, scoped  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402
from engine.sync.pipeline import SyncPipeline  # noqa: E402
from engine.sync.store import SyncConfig  # noqa: E402

BASE_YAML = ROOT / "ontology" / "supply_chain.yaml"
NOW = datetime.now(UTC)
T0 = NOW - timedelta(days=5)
PRINCIPALS = {
    "regional_ops_lead": Principal("regional_ops_lead", "u-lead", "EMEA"),
    "planner": Principal("planner", "u-plan", ""),
    "finance_analyst": Principal("finance_analyst", "u-fin", ""),
    "admin": Principal("admin", "u-admin", ""),
    "restricted": Principal("restricted", "u-res", ""),
}


def custom_yaml(tmpdir: Path) -> Path:
    d = yaml.safe_load(BASE_YAML.read_text())
    d["roles"]["restricted"] = {
        "description": "test role: suppliers with country XX are hidden",
        "types": {"Supplier": {"read": True, "where": "country != 'XX'"}, "Shipment": {"read": True},
                  "Port": {"read": True}, "Carrier": {"read": True}, "Warehouse": {"read": True}},
        "links": ["supplies", "ships_via", "operated_by", "ships_to"],
        "actions": [],
    }
    p = tmpdir / "ontology.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return p


def make_schema(prefix="sec"):
    tmp = Path(tempfile.mkdtemp())
    onto = custom_yaml(tmp)
    ddl = compile_ontology(onto).ddl()
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    admin.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin, ddl)
    return admin, name


@pytest.fixture(scope="session")
def schema():
    admin, name = make_schema()
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


def wipe(c):
    for (t,) in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND "
                          "(tablename LIKE 'obj\\_%%' OR tablename LIKE 'link\\_%%' OR tablename = 'graph_outbox' "
                          "OR tablename = 'inferred_support')").fetchall():
        c.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(sql.Identifier(t)))
    c.commit()


class W:
    """Tiny world builder (owner connection)."""

    def __init__(self, conn):
        self.c = conn
        self.types: dict[uuid.UUID, str] = {}
        self.props: dict[uuid.UUID, dict] = {}
        self.names: dict[str, uuid.UUID] = {}
        self.links: list[tuple[str, uuid.UUID, uuid.UUID]] = []

    def obj(self, key, typ, /, **props):
        i = uuid.uuid4()
        bt.write_object(self.c, typ, i, props, T0, "seed")
        self.types[i], self.props[i], self.names[key] = typ, props, i
        return i

    def link(self, l, a, b):
        a, b = self.names.get(a, a), self.names.get(b, b)
        bt.write_link(self.c, l, a, b, T0, "seed")
        self.links.append((l, a, b))

    def __getitem__(self, k):
        return self.names[k]

    def commit(self):
        self.c.commit()


@contextmanager
def as_role(schema, role, **kw):
    """Fresh connection, one transaction, scoped to the role. Always rolled back."""
    c = connect(schema=schema)
    try:
        with scoped(c, kw.get("principal") or PRINCIPALS[role]):
            yield c
    finally:
        c.rollback()
        c.close()


def build_pipeline(schema, onto_cfg=None) -> SyncPipeline:
    p = SyncPipeline(schema=schema, min_reason_interval_s=0)
    p.sync()
    p.maybe_reason(force=True)
    return p


@pytest.fixture(scope="session")
def world(schema):
    c = connect(schema=schema)
    wipe(c)
    w = W(c)
    # ports / warehouses
    w.obj("p_emea", "Port", name="Rotterdam", region="EMEA", status="open")
    w.obj("p_emea2", "Port", name="Hamburg", region="EMEA", status="open")
    w.obj("p_apac", "Port", name="Singapore", region="APAC", status="open")
    w.obj("p_amer", "Port", name="Houston", region="AMER", status="open")
    w.obj("p_moved", "Port", name="Moved", region="EMEA", status="open")
    w.obj("w_emea", "Warehouse", name="W-EMEA", region="EMEA")
    w.obj("w_apac", "Warehouse", name="W-APAC", region="APAC")
    w.obj("w_emea2", "Warehouse", name="W-EMEA2", region="EMEA")
    w.obj("car", "Carrier", name="Maersk")
    # shipments: s1 -> EMEA (via emea+apac), s2 -> APAC (hidden to lead), s4 -> EMEA via apac only
    w.obj("s1", "Shipment", ref="S1", status="in_transit")
    w.obj("s2", "Shipment", ref="S2", status="in_transit")
    w.obj("s4", "Shipment", ref="S4", status="in_transit")
    w.link("ships_to", "s1", "w_emea"); w.link("ships_to", "s2", "w_apac"); w.link("ships_to", "s4", "w_emea2")
    w.link("ships_via", "s1", "p_emea"); w.link("ships_via", "s1", "p_apac")
    w.link("ships_via", "s2", "p_emea"); w.link("ships_via", "s2", "p_apac")
    w.link("ships_via", "s4", "p_apac"); w.link("ships_via", "s1", "p_moved")
    w.link("operated_by", "s1", "car"); w.link("operated_by", "s2", "car")
    # invoices (finance only)
    w.obj("sup_a", "Supplier", name="A", country="DE")
    w.obj("inv1", "Invoice", ref="INV-1", amount=10.0, status="open")
    w.link("invoices_for", "inv1", "s1"); w.link("billed_by", "inv1", "sup_a")
    # supplier chain a -> b(hidden for `restricted`) -> c -> d ; a -> e -> d ; f isolated chain via hidden g
    w.obj("sup_b", "Supplier", name="B", country="XX")
    w.obj("sup_c", "Supplier", name="C", country="FR")
    w.obj("sup_d", "Supplier", name="D", country="FR")
    w.obj("sup_e", "Supplier", name="E", country="FR")
    w.obj("sup_f", "Supplier", name="F", country="FR")
    w.obj("sup_g", "Supplier", name="G", country="XX")
    w.obj("sup_h", "Supplier", name="H", country="FR")
    for a, b in (("sup_a", "sup_b"), ("sup_b", "sup_c"), ("sup_c", "sup_d"), ("sup_a", "sup_e"), ("sup_e", "sup_d"),
                 ("sup_f", "sup_g"), ("sup_g", "sup_h")):
        w.link("supplies", a, b)
    w.commit()
    # history: p_moved leaves EMEA (now APAC): older versions are EMEA
    bt.write_object(c, "Port", w["p_moved"], {"name": "Moved", "region": "APAC", "status": "open"},
                    NOW - timedelta(days=1), "seed")
    c.commit()
    w.commit_time_past = NOW - timedelta(days=3)
    w.pipe = build_pipeline(schema)
    w.schema = schema
    yield w
    c.close()
