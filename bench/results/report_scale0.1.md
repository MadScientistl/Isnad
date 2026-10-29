# Phase 1 feasibility gate report (seed 1, scale 0.1)

Machine: macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.14.4; versions: {'reasonable': '0.4.4', 'pyoxigraph': '0.5.13', 'pyshacl': '0.40.2', 'owlrl': '7.6.2'}

Dataset: 10000 objects, 44874 edges ({'Supplier': 4000, 'Port': 30, 'Warehouse': 200, 'Carrier': 70, 'Shipment': 5700}; {'supplies': 10674, 'operated_by': 5700, 'ships_via': 28500}); 2 supplier networks contain a cycle; 10 long supplies chains (120 deep at scale 1).

## Verdict vs thresholds (worst of all runs; thresholds not tuned)

| Check | Threshold | Worst observed | Result |
|---|---|---|---|
| reasonable materialization wall time | < 60 s | 2.81 s | PASS |
| 4-hop SPARQL p95 (all variants, cold and warm) | < 2 s | 0.157 s | PASS |
| subgraph SHACL (~1k nodes) | < 10 s | 0.14 s | PASS |
| deliberate SHACL violation detected in every run | yes | True | PASS |
| all stages completed in all 5 runs | yes | True | PASS |

## 1. Load and rebuild times

- Postgres bulk load (bulk_load_objects/links, COPY): objects 0.16 s, links 0.72 s, total 0.88 s
- Oxigraph load by consuming the 54874 outbox rows (incremental path): 1.38 s, peak RSS 176 MB
- Full rebuild from current views (per run): 1.06, 4.30, 4.31, 4.29, 4.26 s; quads 54874

## 2. Materialization (reasonable, OWL 2 RL, ontology + instance graph)

| run | wall s | peak RSS MB | inferred triples | base triples | phases (dump/load/reason/diff+write) |
|---|---|---|---|---|---|
| 1 | 2.55 | 794 | 163642 | 54899 | 0.19/0.19/1.06/1.12   |
| 2 | 2.79 | 790 | 163642 | 54899 | 0.22/0.20/1.09/1.28   |
| 3 | 2.75 | 797 | 163642 | 54899 | 0.21/0.21/1.08/1.25   |
| 4 | 2.81 | 839 | 163642 | 54899 | 0.23/0.23/1.07/1.28   |
| 5 | 2.78 | 827 | 163642 | 54899 | 0.23/0.22/1.09/1.24   |

Only triples whose subject is an instance are kept as inferred (ontology-level closure and owl:Thing noise dropped, count in results.json).

### Correctness cross-check (2000 objects, 9086 edges)

- reasonable inferred 24988 (0.300 s); owlrl inferred 24988 (18.831 s), ignoring 2000 reflexive owl:sameAs triples that only owlrl emits
- reasonable == owlrl: **True** (reasonable-only 0, owlrl-only 0)
- reasonable == independent BFS transitive closure + inverse: **True** (missing 0, extra 0); owlrl == independent: True

## 3. SPARQL 4-hop (asserted graph; 40 random start nodes per variant; cold = first execution per node, warm = repeat)

Process-cold: the OS page cache is not dropped, so 'cold' = first touch per start node in a freshly opened store.

| run | variant | cold median / p95 / max s | warm median / p95 / max s | first query after open s | result size median / max |
|---|---|---|---|---|---|
| 1 | fixed_4hop | 0.0002 / 0.0003 / 0.0005 | 0.0002 / 0.0003 / 0.0003 | 0.0003 | 4.0 / 15 |
| 1 | plus_transitive | 0.0001 / 0.0003 / 0.0004 | 0.0001 / 0.0002 / 0.0004 | 0.0003 | 11.0 / 108 |
| 1 | mixed_4hop_shipment | 0.1516 / 0.1564 / 0.1674 | 0.1507 / 0.1560 / 0.1565 | 0.1674 | 5700.0 / 5700 |
| 1 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 9.5 / 108 |
| 2 | fixed_4hop | 0.0002 / 0.0003 / 0.0004 | 0.0002 / 0.0003 / 0.0003 | 0.0002 | 4.0 / 15 |
| 2 | plus_transitive | 0.0001 / 0.0003 / 0.0005 | 0.0001 / 0.0002 / 0.0004 | 0.0003 | 11.0 / 108 |
| 2 | mixed_4hop_shipment | 0.1499 / 0.1558 / 0.1617 | 0.1500 / 0.1542 / 0.1550 | 0.1617 | 5700.0 / 5700 |
| 2 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 9.5 / 108 |
| 3 | fixed_4hop | 0.0002 / 0.0004 / 0.0005 | 0.0002 / 0.0003 / 0.0003 | 0.0003 | 4.0 / 15 |
| 3 | plus_transitive | 0.0001 / 0.0003 / 0.0004 | 0.0001 / 0.0002 / 0.0004 | 0.0003 | 11.0 / 108 |
| 3 | mixed_4hop_shipment | 0.1489 / 0.1551 / 0.1762 | 0.1491 / 0.1532 / 0.1562 | 0.1762 | 5700.0 / 5700 |
| 3 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 9.5 / 108 |
| 4 | fixed_4hop | 0.0002 / 0.0004 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0003 | 4.0 / 15 |
| 4 | plus_transitive | 0.0001 / 0.0003 / 0.0006 | 0.0001 / 0.0002 / 0.0005 | 0.0003 | 11.0 / 108 |
| 4 | mixed_4hop_shipment | 0.1469 / 0.1545 / 0.1789 | 0.1482 / 0.1529 / 0.1540 | 0.1789 | 5700.0 / 5700 |
| 4 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 9.5 / 108 |
| 5 | fixed_4hop | 0.0002 / 0.0003 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0003 | 4.0 / 15 |
| 5 | plus_transitive | 0.0001 / 0.0003 / 0.0005 | 0.0001 / 0.0002 / 0.0005 | 0.0003 | 11.0 / 108 |
| 5 | mixed_4hop_shipment | 0.1502 / 0.1573 / 0.1797 | 0.1502 / 0.1555 / 0.1576 | 0.1797 | 5700.0 / 5700 |
| 5 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 9.5 / 108 |

## 4. pySHACL on a changed subgraph (330 random shipments + their ports/carriers, properties hydrated from Postgres)

| run | nodes | triples | clean s (conforms) | after deliberate violation s | detected (exactly the victim) |
|---|---|---|---|---|---|
| 1 | 430 | 3260 | 0.14 (True) | 0.11 | True |
| 2 | 430 | 3260 | 0.12 (True) | 0.10 | True |
| 3 | 430 | 3260 | 0.13 (True) | 0.10 | True |
| 4 | 430 | 3260 | 0.13 (True) | 0.10 | True |
| 5 | 430 | 3260 | 0.14 (True) | 0.10 | True |

## 5. 100 edits then resync + re-materialize

- 100 edits in Postgres: 0.25 s; outbox rows 96; resync 0.008 s (added 46, removed 25); full re-materialization 3.39 s; **total 3.40 s**; peak RSS 662 MB. Re-materialization is a full recompute (no incremental path used).

## 6. Stability

5 consecutive runs (rebuild, materialize, queries, SHACL each in a fresh process). Materialization s: 2.55, 2.79, 2.75, 2.81, 2.78; peak RSS MB: 794, 790, 797, 839, 827.

## Caveats

- Synthetic data; supplier networks are bounded (30 nodes) so transitive closure stays tractable; a real giant connected supply graph would blow up quadratically.
- The supplies 4-hop/`+` queries are cheap by construction (supplier networks of 30 nodes, results <= ~19 rows) and random starts almost never land on the 10 long chains; the mixed Shipment->Port->Shipment->Carrier->Shipment 4-hop (about 57k results) is the heavy one. Long-chain `+` latency was not specifically measured.
- Rebuild runs 2-5 are about 2x run 1 because rebuild first clears the existing asserted+inferred graphs (RocksDB clear_graph is slow: seconds per 1e5 quads). Run 1 hit a store with no inferred graph. Materialization writes only the delta of the inferred graph for the same reason.
- Re-materialization after edits is a full recompute (reasonable's incremental update_graph was not evaluated); at scale 1 it was slower than the first materialization because the delta diff reads the old inferred graph into Python.
- Inferred provenance is graph-level (inferred graph + derivedBy + dataVersion); per-edge rule provenance is not implemented (RDF-star unverified).
- The store holds ids/types/edges only; SHACL property shapes need properties hydrated from Postgres (done in the benchmark).
- Memory/time measured on one laptop with Docker running; page cache not dropped.
