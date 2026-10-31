"""Ontology-generator agent (Phase 6): profile -> proposal. Public API: generate(), GenCaps, GenResult, cassettes."""
from .cassette import RecordingModel, Resolver, load_cassette  # noqa: F401
from .generate import GenResult, generate  # noqa: F401
from .loop import GenCaps  # noqa: F401
