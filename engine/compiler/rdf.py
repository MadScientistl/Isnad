from __future__ import annotations

from rdflib import BNode, Graph, Literal, Namespace, URIRef, RDF, RDFS, OWL, XSD
from rdflib.collection import Collection

from .model import GEO_TYPES, Ontology

SH = Namespace("http://www.w3.org/ns/shacl#")
XSD_TYPES = {"string": XSD.string, "int": XSD.integer, "float": XSD.double,
             "bool": XSD.boolean, "timestamp": XSD.dateTime, "enum": XSD.string}
GEO = Namespace("http://www.opengis.net/ont/geosparql#")


def _graph(o: Ontology) -> tuple[Graph, Namespace]:
    g = Graph()
    ns = Namespace(o.namespace)
    g.bind("sc", ns)
    return g, ns


def _ser(g: Graph) -> str:
    return g.serialize(format="turtle")


def owl_turtle(o: Ontology) -> str:
    g, ns = _graph(o)
    g.bind("owl", OWL); g.bind("rdfs", RDFS)
    g.bind("geo", GEO)
    g.add((URIRef(o.namespace.rstrip("#/")), RDF.type, OWL.Ontology))
    for n in sorted(set(o.classes) | set(o.object_types)):
        c = ns[n]
        g.add((c, RDF.type, OWL.Class))
        p = o.parent_of(n)
        if p:
            g.add((c, RDFS.subClassOf, ns[p]))
    for n, t in sorted(o.object_types.items()):
        for pr in t.properties:
            if pr.type in GEO_TYPES:
                g.add((ns[pr.name], RDF.type, OWL.DatatypeProperty))
                g.add((ns[pr.name], RDFS.range, GEO.wktLiteral))
    for n, l in sorted(o.link_types.items()):
        p = ns[n]
        g.add((p, RDF.type, OWL.ObjectProperty))
        g.add((p, RDFS.domain, ns[l.src]))
        g.add((p, RDFS.range, ns[l.dst]))
        if l.transitive:
            g.add((p, RDF.type, OWL.TransitiveProperty))
        if l.inverse:
            i = ns[l.inverse]
            g.add((i, RDF.type, OWL.ObjectProperty))
            g.add((i, RDFS.domain, ns[l.dst]))
            g.add((i, RDFS.range, ns[l.src]))
            g.add((i, OWL.inverseOf, p))
            g.add((p, OWL.inverseOf, i))
            if l.transitive:
                g.add((i, RDF.type, OWL.TransitiveProperty))
    return _ser(g)


def shacl_turtle(o: Ontology) -> str:
    g, ns = _graph(o)
    g.bind("sh", SH); g.bind("xsd", XSD)
    shapes: dict[str, URIRef] = {}

    def shape(name: str) -> URIRef:
        if name not in shapes:
            s = ns[f"{name}Shape"]
            shapes[name] = s
            g.add((s, RDF.type, SH.NodeShape))
            g.add((s, SH.targetClass, ns[name]))
        return shapes[name]

    def prop_shape(node: URIRef, path) -> BNode:
        b = BNode()
        g.add((node, SH.property, b))
        g.add((b, SH.path, path))
        return b

    for n, t in sorted(o.object_types.items()):
        s = shape(n)
        for p in t.properties:
            if p.type in GEO_TYPES:  # geometry is not mirrored into the graph store
                continue
            b = prop_shape(s, ns[p.name])
            g.add((b, SH.maxCount, Literal(1)))
            if p.type == "enum":
                lst = BNode()
                Collection(g, lst, [Literal(v) for v in p.enum])
                g.add((b, SH["in"], lst))
            g.add((b, SH.datatype, XSD_TYPES[p.type]))
            if p.required:
                g.add((b, SH.minCount, Literal(1)))
    for n, l in sorted(o.link_types.items()):
        fwd_max = l.cardinality in ("many-to-one", "one-to-one")
        inv_max = l.cardinality in ("one-to-many", "one-to-one")
        if l.required or fwd_max:
            b = prop_shape(shape(l.src), ns[n])
            if l.required:
                g.add((b, SH.minCount, Literal(1)))
            if fwd_max:
                g.add((b, SH.maxCount, Literal(1)))
        if inv_max:
            ip = BNode()
            g.add((ip, SH.inversePath, ns[n]))
            b = prop_shape(shape(l.dst), ip)
            g.add((b, SH.maxCount, Literal(1)))
    return _ser(g)
