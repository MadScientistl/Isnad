"""Effective agent permissions = INTERSECTION of
  (human principal's RLS for reads) AND role.actions AND role.agent.allowlist (propose) AND role.agent.tools.
Never a union. Anything denied is returned to the model as a tool error and recorded as a `denied` event.
"""
from __future__ import annotations

from engine.compiler.model import AGENT_TOOLS, GENERIC_ACTIONS

# Phase 5 scenario tools. The compiler's AGENT_TOOLS does not list them yet, so the YAML cannot name them; until it
# does, a role whose YAML lists no scenario tool gets these defaults (still intersected with the role's agent
# allowlist for scenario_apply). A role that lists ANY scenario tool in its YAML gets exactly the YAML list.
SCENARIO_TOOLS = ("scenario_open", "scenario_apply", "scenario_simulate", "scenario_compare")
SCENARIO_DEFAULTS: dict[str, tuple[str, ...]] = {}   # optional explicit per-role overrides (none needed)
SCENARIO_DEFAULT_FULL = SCENARIO_TOOLS


def default_scenario_tools(model, role: str, acts: tuple[str, ...]) -> tuple[str, ...]:
    """Scenario tools for a full agent whose YAML lists none, derived from what the role can do (never from its
    name): every proposing agent may open + compare; applying needs an action it may propose; simulating
    forward needs an ontology that declares a simulation."""
    if role in SCENARIO_DEFAULTS:
        return SCENARIO_DEFAULTS[role]
    out = ["scenario_open", "scenario_compare"]
    if acts:
        out.insert(1, "scenario_apply")
        if getattr(model, "sim", None) is not None:
            out.insert(2, "scenario_simulate")
    return tuple(out)


class PolicyDenied(Exception):
    def __init__(self, kind: str, name: str, reason: str):
        super().__init__(reason)
        self.kind, self.name, self.reason = kind, name, reason


def _model(m):
    return getattr(m, "model", m)


def allowed_actions(model, role: str) -> tuple[str, ...]:
    """Actions the role's agent may propose: allowlist AND role may execute; generic write actions never."""
    model = _model(model)
    return tuple(a for a in model.agent_allowlist(role)
                 if a not in GENERIC_ACTIONS and a in model.actions and model.role_can_execute(role, a))


def allowed_tools(model, role: str) -> tuple[str, ...]:
    model = _model(model)
    acts = allowed_actions(model, role)
    yaml_tools = tuple(model.agent_tools(role))
    base = tuple(t for t in yaml_tools if t in AGENT_TOOLS and (t != "propose_action" or acts))
    scn = tuple(t for t in yaml_tools if t in SCENARIO_TOOLS)
    if not scn and "propose_action" in base:   # defaults only for full agents (a deliberately narrowed role stays narrow)
        scn = default_scenario_tools(model, role, acts)
    scn = tuple(t for t in scn if t != "scenario_apply" or acts)
    return base + tuple(t for t in scn if t not in base)


def check_tool(ctx, name: str) -> None:
    if name not in allowed_tools(ctx.model, ctx.principal.role):
        raise PolicyDenied("tool", str(name)[:80], "tool is not available to this agent")


def check_action(ctx, name: str) -> None:
    if not isinstance(name, str) or name not in allowed_actions(ctx.model, ctx.principal.role):
        raise PolicyDenied("action", str(name)[:80], "action is not in this agent's proposal allowlist")
