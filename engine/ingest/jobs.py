"""In-process job registry for the ingest API (model proposals, repairs, imports).

Same pattern as the agent run registry: caps are checked under one lock that also registers the job, so concurrent
requests cannot both pass. Limits reuse the agent env knobs (read per request): ISNAD_AGENT_MAX_CONCURRENT (per user),
ISNAD_AGENT_RUNS_PER_MIN (per user), ISNAD_AGENT_MAX_GLOBAL. Jobs keep an event log that SSE clients follow; at most
``MAX_STREAMS_PER_JOB`` open streams per job and ``MAX_STREAMS_GLOBAL`` overall. Finished jobs are evicted oldest-first.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Callable

log = logging.getLogger("isnad.ingest_jobs")

MAX_STREAMS_PER_JOB = 4
MAX_STREAMS_GLOBAL = 16
KEEP_FINISHED = 200
KEEP_PER_USER = 50
MAX_EVENTS = 500
PAYLOAD_CHARS = 2000


class JobLimit(Exception):
    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.message, self.retry_after = message, retry_after


def _env(name: str, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def limits() -> dict:
    return {"max_concurrent": _env("ISNAD_AGENT_MAX_CONCURRENT", 1, int),
            "runs_per_minute": _env("ISNAD_AGENT_RUNS_PER_MIN", 6, int),
            "max_global": _env("ISNAD_AGENT_MAX_GLOBAL", 4, int),
            "daily_usd": _env("ISNAD_AGENT_DAILY_USD", 5.0, float),
            "global_daily_usd": _env("ISNAD_INGEST_GLOBAL_DAILY_USD", _env("ISNAD_AGENT_DAILY_USD", 5.0, float) * 2, float)}


def _clip(payload):
    try:
        import json
        s = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return {"note": "unserialisable"}
    return payload if len(s) <= PAYLOAD_CHARS else {"truncated": True, "preview": s[:PAYLOAD_CHARS]}


class Job:
    def __init__(self, kind: str, user: str, job_id: str | None = None):
        self.id = job_id or str(uuid.uuid4())
        self.kind, self.user = kind, user
        self.status = "queued"            # queued | running | done | failed | cancelled
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.cancel = threading.Event()
        self.result: dict | None = None
        self.error: dict | None = None    # {code, detail}: never carries untrusted data
        self.proposal_id: str | None = None
        self.cost_usd = 0.0
        self.created = time.monotonic()
        self.streams = 0
        self.thread: threading.Thread | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def push(self, kind: str, payload: dict | None = None) -> dict:
        with self.cond:
            ev = {"seq": len(self.events) + 1, "kind": kind, "payload": _clip(payload or {})}
            if len(self.events) < MAX_EVENTS:
                self.events.append(ev)
            self.cond.notify_all()
            return ev

    def finish(self, status: str, *, result: dict | None = None, error: dict | None = None) -> None:
        with self.cond:
            if self.finished:
                return
            self.status, self.result, self.error = status, result, error
            self.events.append({"seq": len(self.events) + 1, "kind": "finished",
                                "payload": _clip({"status": status, "error": error, "result": result})})
            self.cond.notify_all()

    def to_dict(self, after: int = 0, limit: int = 500) -> dict:
        with self.cond:
            evs = [e for e in self.events if e["seq"] > after][:limit]
            return {"id": self.id, "kind": self.kind, "status": self.status, "events": evs,
                    "proposal_id": self.proposal_id, "cost_usd": round(self.cost_usd, 6),
                    "result": self.result, "error": self.error}


class JobRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.starts: dict[str, deque] = {}
        self.streams = 0

    def _evict(self) -> None:
        done = [j for j in self.jobs.values() if j.finished]
        for j in sorted(done, key=lambda j: j.created)[: max(0, len(done) - KEEP_FINISHED)]:
            self.jobs.pop(j.id, None)
        by_user: dict[str, list[Job]] = {}
        for j in self.jobs.values():
            if j.finished:
                by_user.setdefault(j.user, []).append(j)
        for js in by_user.values():
            for j in sorted(js, key=lambda j: j.created)[: max(0, len(js) - KEEP_PER_USER)]:
                self.jobs.pop(j.id, None)

    def get(self, job_id: str, user: str | None = None) -> Job | None:
        """The job, or None. With ``user`` only the job's owner gets it (everyone else sees "not found")."""
        j = self.jobs.get(job_id)
        if j is not None and user is not None and j.user != user:
            return None
        return j

    def completed(self, kind: str, user: str, *, result: dict | None = None, proposal_id: str | None = None,
                  cost_usd: float = 0.0) -> Job:
        """A job that already finished (synchronous work reported through the same API)."""
        j = Job(kind, user)
        j.proposal_id, j.cost_usd = proposal_id, cost_usd
        j.status = "running"
        j.finish("done", result=result)
        with self.lock:
            self.jobs[j.id] = j
            self._evict()
        return j

    def start(self, kind: str, user: str, work: Callable[[Job], None], *, lim: dict | None = None,
              job_id: str | None = None) -> Job:
        lim = lim or limits()
        now = time.monotonic()
        with self.lock:
            active = [j for j in self.jobs.values() if not j.finished]
            if sum(1 for j in active if j.user == user) >= lim["max_concurrent"]:
                raise JobLimit("a job is already running for this user")
            if len(active) >= lim["max_global"]:
                raise JobLimit("the ingest workers are busy; try again shortly")
            dq = self.starts.setdefault(user, deque())
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= lim["runs_per_minute"]:
                raise JobLimit("too many jobs started this minute", max(1, int(60 - (now - dq[0]))))
            dq.append(now)
            job = Job(kind, user, job_id)
            self.jobs[job.id] = job
            self._evict()

        def run():
            job.status = "running"
            try:
                work(job)
                if not job.finished:
                    job.finish("done", result=job.result)
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None)
                log.warning("ingest job %s failed: %s", job.kind, type(e).__name__)   # class only: no data in logs
                extra = getattr(e, "extra", None)
                job.finish("failed", error={"code": code if isinstance(code, str) else "job_failed",
                                            "detail": getattr(e, "message", None) or "the job failed",
                                            **(extra if isinstance(extra, dict) else {})})

        job.thread = threading.Thread(target=run, name=f"ingest-{kind}-{job.id[:8]}", daemon=True)
        job.thread.start()
        return job

    def open_stream(self, job: Job) -> bool:
        with self.lock:
            if job.streams >= MAX_STREAMS_PER_JOB or self.streams >= MAX_STREAMS_GLOBAL:
                return False
            job.streams += 1
            self.streams += 1
            return True

    def close_stream(self, job: Job) -> None:
        with self.lock:
            job.streams = max(0, job.streams - 1)
            self.streams = max(0, self.streams - 1)
