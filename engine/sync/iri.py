"""IRI scheme (contract): objects <ns>obj/<uuid>, classes <ns><Type>, links <ns><link>."""
from __future__ import annotations

from pyoxigraph import NamedNode

DEFAULT_NS = "https://example.org/sc#"
RDF_TYPE = NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")

# Named graphs. Asserted = what Postgres says; inferred = reasoner output only.
G_ASSERTED = NamedNode("urn:amarh:g:asserted")
G_INFERRED = NamedNode("urn:amarh:g:inferred")
G_ONTOLOGY = NamedNode("urn:amarh:g:ontology")
G_META = NamedNode("urn:amarh:g:meta")
SYNC_SUBJ = NamedNode("urn:amarh:sync")
LAST_SEQ = NamedNode("urn:amarh:lastSeq")
DATA_VERSION = NamedNode("urn:amarh:dataVersion")
INFERRED_FLAG = NamedNode("urn:amarh:isInferredGraph")
DERIVED_BY = NamedNode("urn:amarh:derivedBy")


def obj_iri(ns: str, id) -> NamedNode:
    return NamedNode(f"{ns}obj/{id}")


def class_iri(ns: str, type_: str) -> NamedNode:
    return NamedNode(f"{ns}{type_}")


def link_iri(ns: str, link: str) -> NamedNode:
    return NamedNode(f"{ns}{link}")
