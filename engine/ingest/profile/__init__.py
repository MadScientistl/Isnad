"""Deterministic data profiler for ontology auto-generation.

``profile_source(source, caps=..., seed=1) -> Profile`` (a JSON-serialisable dict with a ``profile_hash``) and
``baseline_proposal(profile) -> dict`` (heuristic Proposal, no LLM). All data-derived strings are sanitised.
"""
from .baseline import baseline_proposal
from .caps import Caps
from .core import Profile, profile_source

__all__ = ["profile_source", "baseline_proposal", "Profile", "Caps"]
