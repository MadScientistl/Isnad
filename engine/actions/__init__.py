"""Action + audit layer. Every write goes through `apply_action`.

Internal optional params (allowed in addition to the YAML registry; they let the simulation
supply deterministic ids): create_object `id`; close_port `event_id`; issue_invoice
`invoice_id` (uuid) and `ref` (string). All default to uuid4 / a generated ref.
"""
from .core import ActionResult, apply_action, ensure_audit_schema  # noqa: F401

__all__ = ["ActionResult", "apply_action", "ensure_audit_schema"]
