"""Action engine + audit layer (Phase 3). Every write goes through `execute`.

See engine/actions/core.py for the pipeline and docs/contracts.md for the contract.
"""
from .core import (ActionResult, ApprovalRequired, CriteriaFailed, NotFound, PermissionDenied,  # noqa: F401
                   ProposalExpired, StaleProposal, RollbackConflict, RollbackRefused, SIM, decide, dry_run,
                   ensure_audit_schema, execute, make_principal, propose, rollback)

__all__ = ["ProposalExpired", "StaleProposal", "ActionResult", "ApprovalRequired", "CriteriaFailed", "NotFound", "PermissionDenied",
           "RollbackConflict", "RollbackRefused", "SIM", "decide", "dry_run",
           "ensure_audit_schema", "execute", "make_principal", "propose", "rollback"]
