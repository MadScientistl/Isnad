"""YAML ontology compiler: DDL, OWL, SHACL, Pydantic, TypeScript."""
from __future__ import annotations

import hashlib
import types
from dataclasses import dataclass
from pathlib import Path

from .model import Ontology, OntologyError, load_ontology, parse_ontology
from .ddl import generate_base_ddl, generate_ddl, generate_policy_ddl
from .rdf import owl_turtle, shacl_turtle
from .codegen import pydantic_source, ts_source

__all__ = ["compile_ontology", "CompiledOntology", "OntologyError", "write_outputs"]


@dataclass
class CompiledOntology:
    model: Ontology
    yaml_text: str

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.yaml_text.encode()).hexdigest()

    def ddl(self) -> str:
        return generate_ddl(self.model)

    def base_ddl(self) -> str:
        return generate_base_ddl(self.model)

    def policy_ddl(self) -> str:
        """Roles, RLS policies, visibility helpers, masking views and grants (also part of ddl())."""
        return generate_policy_ddl(self.model)

    def owl_turtle(self) -> str:
        return owl_turtle(self.model)

    def shacl_turtle(self) -> str:
        return shacl_turtle(self.model)

    def pydantic_source(self) -> str:
        return pydantic_source(self.model)

    def pydantic_models(self) -> types.ModuleType:
        """Exec the generated source; returns a module with one model class per object type."""
        mod = types.ModuleType("amarh_models")
        import sys
        sys.modules["amarh_models"] = mod
        exec(compile(self.pydantic_source(), "<amarh_models>", "exec"), mod.__dict__)
        return mod

    def ts_sdk(self) -> str:
        return ts_source(self.model)


def compile_ontology(path) -> CompiledOntology:
    model, text = load_ontology(path)
    return CompiledOntology(model, text)


def write_outputs(c: CompiledOntology, outdir, sdk_dir=None) -> list[Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    files = {"schema.sql": c.ddl(), "ontology.ttl": c.owl_turtle(), "shapes.ttl": c.shacl_turtle(),
             "models.py": c.pydantic_source(), "client.ts": c.ts_sdk()}
    paths = []
    for n, t in files.items():
        (out / n).write_text(t)
        paths.append(out / n)
    if sdk_dir is not None:
        sdk = Path(sdk_dir)
        sdk.mkdir(parents=True, exist_ok=True)
        (sdk / "client.ts").write_text(files["client.ts"])
        paths.append(sdk / "client.ts")
    return paths
