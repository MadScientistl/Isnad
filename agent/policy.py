"""Effective agent permissions = INTERSECTION of
  (human principal's RLS for reads) AND role.actions AND role.agent.allowlist (propose) AND role.agent.tools.
Never a union. Anything denied is returned to the model as a tool error and recorded as a `denied` event.
"""
from __future__ import annotations

from engine.compiler.model import AGENT_TOOLS, GENERIC_ACTIONS


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
    return tuple(t for t in model.agent_tools(role) if t in AGENT_TOOLS and (t != "propose_action" or acts))


def check_tool(ctx, name: str) -> None:
    if name not in allowed_tools(ctx.model, ctx.principal.role):
        raise PolicyDenied("tool", str(name)[:80], "tool is not available to this agent")


def check_action(ctx, name: str) -> None:
    if not isinstance(name, str) or name not in allowed_actions(ctx.model, ctx.principal.role):
        raise PolicyDenied("action", str(name)[:80], "action is not in this agent's proposal allowlist")
