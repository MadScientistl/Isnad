# Phase 1 feasibility gate report (seed 1, scale 1.0)

Machine: macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.14.4; versions: {'reasonable': '0.4.4', 'pyoxigraph': '0.5.13', 'pyshacl': '0.40.2', 'owlrl': '7.6.2'}

Dataset: 100000 objects, 468357 edges ({'Supplier': 40000, 'Port': 300, 'Warehouse': 2000, 'Carrier': 700, 'Shipment': 57000}; {'supplies': 126357, 'operated_by': 57000, 'ships_via': 285000}); 38 supplier networks contain a cycle; 10 long supplies chains (120 deep at scale 1).

## Verdict vs thresholds (worst of all runs; thresholds not tuned)

| Check | Threshold | Worst observed | Result |
|---|---|---|---|
| reasonable materialization wall time | < 60 s | 18.40 s | PASS |
| 4-hop SPARQL p95 (all variants, cold and warm) | < 2 s | 0.217 s | PASS |
| subgraph SHACL (~1k nodes) | < 10 s | 0.28 s | PASS |
| deliberate SHACL violation detected in every run | yes | True | PASS |
| all stages completed in all 5 runs | yes | True | PASS |

## 1. Load and rebuild times

- Postgres bulk load (bulk_load_objects/links, COPY): objects 1.84 s, links 9.48 s, total 11.32 s
- Oxigraph load by consuming the 568357 outbox rows (incremental path): 23.84 s, peak RSS 708 MB
- Full rebuild from current views (per run): 22.68, 48.21, 49.32, 48.07, 46.86 s; quads 568357

## 2. Materialization (reasonable, OWL 2 RL, ontology + instance graph)

| run | wall s | peak RSS MB | inferred triples | base triples | phases (dump/load/reason/diff+write) |
|---|---|---|---|---|---|
| 1 | 18.40 | 2476 | 779511 | 568404 | 2.37/3.01/5.95/7.07   |
| 2 | 18.13 | 2556 | 779511 | 568404 | 2.37/3.08/5.76/6.93   |
| 3 | 18.35 | 2829 | 779511 | 568404 | 2.37/3.05/5.92/7.01   |
| 4 | 17.97 | 2759 | 779511 | 568404 | 2.35/3.00/5.65/6.97   |
| 5 | 17.67 | 3021 | 779511 | 568404 | 2.31/2.95/5.62/6.79   |

Only triples whose subject is an instance are kept as inferred (ontology-level closure and owl:Thing noise dropped, count in results.json).

### Correctness cross-check (2000 objects, 9086 edges)

- reasonable inferred 24988 (0.307 s); owlrl inferred 24988 (18.967 s), ignoring 2000 reflexive owl:sameAs triples that only owlrl emits
- reasonable == owlrl: **True** (reasonable-only 0, owlrl-only 0)
- reasonable == independent BFS transitive closure + inverse: **True** (missing 0, extra 0); owlrl == independent: True

## 3. SPARQL 4-hop (asserted graph; 40 random start nodes per variant; cold = first execution per node, warm = repeat)

Process-cold: the OS page cache is not dropped, so 'cold' = first touch per start node in a freshly opened store.

| run | variant | cold median / p95 / max s | warm median / p95 / max s | first query after open s | result size median / max |
|---|---|---|---|---|---|
| 1 | fixed_4hop | 0.0003 / 0.0006 / 0.0007 | 0.0002 / 0.0004 / 0.0006 | 0.0007 | 6.0 / 14 |
| 1 | plus_transitive | 0.0001 / 0.0003 / 0.0003 | 0.0001 / 0.0001 / 0.0002 | 0.0002 | 8.0 / 19 |
| 1 | mixed_4hop_shipment | 0.1902 / 0.2166 / 0.2268 | 0.1883 / 0.1922 / 0.1931 | 0.2268 | 56929.5 / 57000 |
| 1 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 2 | fixed_4hop | 0.0002 / 0.0004 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0005 | 6.0 / 14 |
| 2 | plus_transitive | 0.0001 / 0.0002 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 2 | mixed_4hop_shipment | 0.1781 / 0.1898 / 0.2087 | 0.1779 / 0.1862 / 0.1878 | 0.2087 | 56929.5 / 57000 |
| 2 | inferred_lookup | 0.0000 / 0.0001 / 0.0002 | 0.0000 / 0.0001 / 0.0001 | 0.0001 | 8.5 / 102 |
| 3 | fixed_4hop | 0.0003 / 0.0005 / 0.0006 | 0.0002 / 0.0004 / 0.0004 | 0.0005 | 6.0 / 14 |
| 3 | plus_transitive | 0.0001 / 0.0002 / 0.0003 | 0.0001 / 0.0001 / 0.0002 | 0.0001 | 8.0 / 19 |
| 3 | mixed_4hop_shipment | 0.1918 / 0.2150 / 0.2208 | 0.1893 / 0.1941 / 0.2044 | 0.2138 | 56929.5 / 57000 |
| 3 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 4 | fixed_4hop | 0.0002 / 0.0005 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0005 | 6.0 / 14 |
| 4 | plus_transitive | 0.0001 / 0.0001 / 0.0002 | 0.0001 / 0.0001 / 0.0002 | 0.0001 | 8.0 / 19 |
| 4 | mixed_4hop_shipment | 0.1711 / 0.1972 / 0.2055 | 0.1700 / 0.1763 / 0.1802 | 0.2055 | 56929.5 / 57000 |
| 4 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 5 | fixed_4hop | 0.0002 / 0.0005 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0005 | 6.0 / 14 |
| 5 | plus_transitive | 0.0001 / 0.0002 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 5 | mixed_4hop_shipment | 0.1763 / 0.1975 / 0.2082 | 0.1777 / 0.1844 / 0.1866 | 0.2082 | 56929.5 / 57000 |
| 5 | inferred_lookup | 0.0000 / 0.0001 / 0.0002 | 0.0000 / 0.0000 / 0.0001 | 0.0002 | 8.5 / 102 |

## 4. pySHACL on a changed subgraph (330 random shipments + their ports/carriers, properties hydrated from Postgres)

| run | nodes | triples | clean s (conforms) | after deliberate violation s | detected (exactly the victim) |
|---|---|---|---|---|---|
| 1 | 903 | 5016 | 0.28 (True) | 0.23 | True |
| 2 | 903 | 5016 | 0.26 (True) | 0.23 | True |
| 3 | 903 | 5016 | 0.28 (True) | 0.26 | True |
| 4 | 903 | 5016 | 0.28 (True) | 0.23 | True |
| 5 | 903 | 5016 | 0.27 (True) | 0.23 | True |

## 5. 100 edits then resync + re-materialize

- 100 edits in Postgres: 0.37 s; outbox rows 100; resync 0.008 s (added 50, removed 25); full re-materialization 19.52 s; **total 19.53 s**; peak RSS 2500 MB. Re-materialization is a full recompute (no incremental path used).

## 6. Stability

5 consecutive runs (rebuild, materialize, queries, SHACL each in a fresh process). Materialization s: 18.40, 18.13, 18.35, 17.97, 17.67; peak RSS MB: 2476, 2556, 2829, 2759, 3021.

## Caveats

- Synthetic data; supplier networks are bounded (30 nodes) so transitive closure stays tractable; a real giant connected supply graph would blow up quadratically.
- The supplies 4-hop/`+` queries are cheap by construction (supplier networks of 30 nodes, results <= ~19 rows) and random starts almost never land on the 10 long chains; the mixed Shipment->Port->Shipment->Carrier->Shipment 4-hop (about 57k results) is the heavy one. Long-chain `+` latency was not specifically measured.
- Rebuild runs 2-5 are about 2x run 1 because rebuild first clears the existing asserted+inferred graphs (RocksDB clear_graph is slow: seconds per 1e5 quads). Run 1 hit a store with no inferred graph. Materialization writes only the delta of the inferred graph for the same reason.
- Re-materialization after edits is a full recompute (reasonable's incremental update_graph was not evaluated); at scale 1 it was slower than the first materialization because the delta diff reads the old inferred graph into Python.
- Inferred provenance is graph-level (inferred graph + derivedBy + dataVersion); per-edge rule provenance is not implemented (RDF-star unverified).
- The store holds ids/types/edges only; SHACL property shapes need properties hydrated from Postgres (done in the benchmark).
- Memory/time measured on one laptop with Docker running; page cache not dropped.
