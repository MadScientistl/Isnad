"""Live smoke test against the real Anthropic API. Skipped unless ANTHROPIC_API_KEY is set (never run in CI)."""
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "agent"))
from agent import AgentContext, AgentRun, Caps, MemorySink  # noqa: E402
from agent.model_anthropic import AnthropicModel  # noqa: E402
from agent_helpers import COMP, link, obj, principal  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")]


def test_planner_tiny_question():
    name = f"ag_live_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(get_dsn(), autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    try:
        c = connect(schema=name)
        port = obj(c, "Port", {"name": "Rotterdam", "status": "closed", "region": "EMEA"})
        ship = obj(c, "Shipment", {"ref": "SH-1", "status": "in_transit"})
        link(c, "ships_via", ship, port)
        c.close()
        ctx = AgentContext(principal("planner"), COMP, lambda: connect(schema=name),
                           caps=Caps(max_turns=4, max_tool_calls=4, max_output_tokens=4000, max_cost_usd=0.05,
                                     max_wall_s=60))
        sink = MemorySink()
        res = AgentRun(ctx, AnthropicModel(), sink).run("Which shipments sail via closed ports? Be brief.")
        assert res.status in ("completed", "capped"), res.error
        assert any(e["kind"] == "tool_call" for e in sink.events)
        assert res.cost_usd < 0.05
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        admin.close()
