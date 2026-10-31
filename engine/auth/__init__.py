"""Minimal real authentication (Phase 6, W2). See docs/contracts.md "Auth"."""
from engine.auth.passwords import hash_password, verify_password
from engine.auth.service import AuthService, Identity, LoginFailed, Throttled, enabled
from engine.auth.users import AuthConfigError

__all__ = ["AuthService", "AuthConfigError", "Identity", "LoginFailed", "Throttled", "enabled",
           "hash_password", "verify_password"]
