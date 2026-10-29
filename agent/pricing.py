"""Price table (USD per 1M tokens) and cost computation from API `usage`. Source: claude-api skill cache 2026-10-06."""
from __future__ import annotations

# model -> (input, output, cache_read, cache_write_5m)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
# unknown model ids are priced at the most expensive known rate (a cost cap must fail safe)
_FALLBACK = tuple(max(p[i] for p in PRICES.values()) for i in range(4))


def cost_usd(model: str, usage: dict) -> float:
    """`usage` keys: input_tokens (uncached), output_tokens, cache_read_input_tokens, cache_creation_input_tokens."""
    pin, pout, pread, pwrite = PRICES.get(model, _FALLBACK)
    return (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout
            + usage.get("cache_read_input_tokens", 0) * pread
            + usage.get("cache_creation_input_tokens", 0) * pwrite) / 1_000_000
