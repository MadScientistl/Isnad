"""Phase 3 demo: the same action allowed for one role, denied for another, with audit.

Run: uv run python scripts/demo_phase3.py   (needs the database; uses a throw-away schema)
"""
import uuid
from datetime import UTC, datetime

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from engine.api.main import create_app
from engine.api.seed import _id, seed
from engine.db import bitemporal as bt, connect, get_dsn

T0 = datetime(2026, 1, 1, tzinfo=UTC)
schema = f"demo3_{uuid.uuid4().hex[:8]}"
admin = psycopg.connect(get_dsn(), autocommit=True)
admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
try:
    c = connect(schema=schema)
    seed(c)
    inv = uuid.uuid4()
    bt.write_object(c, "Invoice", inv, {"ref": "INV-DEMO", "amount": 900.0, "status": "open"}, T0, "demo")
    c.commit()
    rotterdam, singapore = str(_id("Port", "Rotterdam")), str(_id("Port", "Singapore"))
    cl = TestClient(create_app("ontology/supply_chain.yaml", schema=schema, pipeline=False))

    def run(role, action, params):
        r = cl.post(f"/actions/{action}/execute", json={"params": params}, headers={"x-role": role})
        body = r.json()
        msg = body.get("detail") or body.get("status") or body
        print(f"  {role:18} {action:13} -> {r.status_code} {msg}")
        return r

    print("close_port on Rotterdam (EMEA):")
    run("finance_analyst", "close_port", {"port": rotterdam})
    run("planner", "close_port", {"port": rotterdam})
    print("close_port on Singapore (APAC):")
    run("regional_ops_lead", "close_port", {"port": singapore})
    print("flag_invoice:")
    run("planner", "flag_invoice", {"invoice": str(inv), "anomaly": "duplicate"})
    run("finance_analyst", "flag_invoice", {"invoice": str(inv), "anomaly": "duplicate"})
    print("audit feed (admin):")
    for e in cl.get("/executions", headers={"x-role": "admin"}).json()[:8]:
        print(f"  #{e.get('seq')} {e['name']:13} actor={e['actor']:18} role={e.get('role')} status={e['status']}")
finally:
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
