"""Regression tests for the final Phase 6 review: DNS-rebinding guard and strict identifier patterns."""
import pytest

from engine.compiler.model import OntologyError, parse_ontology


def test_foreign_host_header_is_refused(lib):
    ok = lib.get("/health", "admin")
    assert ok.status_code == 200
    r = lib.c.get("/me", headers={"x-role": "admin", "host": "evil.example.com:8000"})
    assert r.status_code == 400, r.text


def test_loopback_hosts_are_served(lib):
    for host in ("localhost:8000", "127.0.0.1:8000"):
        r = lib.c.get("/health", headers={"host": host})
        assert r.status_code == 200, host


@pytest.mark.parametrize("name", ["Foo\n", "foo", "Foo Bar", "Foo;"])
def test_type_name_pattern_rejects_trailing_newline_and_junk(name):
    raw = {"version": 1, "namespace": "https://example.org/t#", "objectTypes": {name: {"properties": {"a": "string"}}}}
    with pytest.raises(OntologyError):
        parse_ontology(raw)


def test_property_name_pattern_rejects_trailing_newline():
    raw = {"version": 1, "namespace": "https://example.org/t#", "objectTypes": {"Foo": {"properties": {"a\n": "string"}}}}
    with pytest.raises(OntologyError):
        parse_ontology(raw)
