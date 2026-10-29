"""Headless run: python -m sim --seed 1 --days 30 [--schema S] [--dsn D]"""
from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta

from .runner import SimRunner


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m sim")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    ap.add_argument("--epoch", default="2026-01-01T00:00:00+00:00")
    a = ap.parse_args()
    epoch = datetime.fromisoformat(a.epoch)
    r = SimRunner(dsn=a.dsn, schema=a.schema, seed=a.seed, epoch=epoch)
    t0 = time.perf_counter()
    r.run_until(r.sim_now + timedelta(days=a.days))
    wall = time.perf_counter() - t0
    w = r.world
    print(json.dumps({"sim_now": r.sim_now.isoformat(), "wall_s": round(wall, 2), "actions": r.actions,
                      "actions_per_s": round(r.actions / wall, 1) if wall else None,
                      "by_action": dict(sorted(w.stats.items())), "active_shipments": len(w.active),
                      "last_error": r.last_error, "digest": r.event_digest()}, indent=2))
    r.close()


main()
