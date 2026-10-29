import sys

from . import OntologyError, compile_ontology, write_outputs


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sdk = None
    if "--sdk" in argv:
        i = argv.index("--sdk")
        if i + 1 >= len(argv):
            print("error: --sdk needs a directory", file=sys.stderr)
            return 2
        sdk = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        print("usage: python -m engine.compiler <ontology.yaml> <outdir> [--sdk <dir>]", file=sys.stderr)
        return 2
    try:
        paths = write_outputs(compile_ontology(argv[0]), argv[1], sdk)
    except OntologyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
