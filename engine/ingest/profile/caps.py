from __future__ import annotations

import os
from dataclasses import asdict, dataclass


def _env_int(env, name: str, default: int) -> int:
    try:
        v = int(env.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Caps:
    max_tables: int = 50
    max_columns: int = 60
    max_rows_profiled: int = 200_000
    max_sample: int = 5_000
    top_k: int = 10

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Caps":
        env = os.environ if env is None else env
        return cls(max_tables=_env_int(env, "ISNAD_INGEST_MAX_TABLES", 50),
                   max_columns=_env_int(env, "ISNAD_INGEST_MAX_COLUMNS", 60),
                   max_rows_profiled=_env_int(env, "ISNAD_INGEST_MAX_ROWS_PROFILED", 200_000),
                   max_sample=_env_int(env, "ISNAD_INGEST_MAX_SAMPLE", 5_000))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def coerce(cls, caps: "Caps | dict | None") -> "Caps":
        if caps is None:
            return cls.from_env()
        if isinstance(caps, Caps):
            return caps
        base = cls()
        return cls(**{k: int(caps.get(k, getattr(base, k))) for k in asdict(base)})
