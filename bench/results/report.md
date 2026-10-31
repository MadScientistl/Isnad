# Phase 1 feasibility gate report (seed 1, scale 1.0)

Machine: macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.14.4; versions: {'reasonable': '0.4.4', 'pyoxigraph': '0.5.13', 'pyshacl': '0.40.2', 'owlrl': '7.6.2'}

Dataset: 100000 objects, 468357 edges ({'Supplier': 40000, 'Port': 300, 'Warehouse': 2000, 'Carrier': 700, 'Shipment': 57000}; {'supplies': 126357, 'operated_by': 57000, 'ships_via': 285000}); 38 supplier networks contain a cycle; 10 long supplies chains (120 deep at scale 1).

## Verdict vs thresholds (worst of all runs; thresholds not tuned)

| Check | Threshold | Worst observed | Result |
|---|---|---|---|
| reasonable materialization wall time | < 60 s | 17.28 s | PASS |
| 4-hop SPARQL p95 (all variants, cold and warm) | < 2 s | 0.183 s | PASS |
| subgraph SHACL (~1k nodes) | < 10 s | 0.30 s | PASS |
| deliberate SHACL violation detected in every run | yes | True | PASS |
| all stages completed in all 5 runs | yes | True | PASS |

## 1. Load and rebuild times

- Postgres bulk load (bulk_load_objects/links, COPY): objects 2.13 s, links 11.53 s, total 13.66 s
- Oxigraph load by consuming the 568357 outbox rows (incremental path): 21.39 s, peak RSS 705 MB
- Full rebuild from current views (per run): 19.76, 41.47, 40.00, 41.57, 40.03 s; quads 568357

## 2. Materialization (reasonable, OWL 2 RL, ontology + instance graph)

| run | wall s | peak RSS MB | inferred triples | base triples | phases (dump/load/reason/diff+write) |
|---|---|---|---|---|---|
| 1 | 17.28 | 2152 | 779511 | 568404 | 2.26/2.55/5.72/6.75   |
| 2 | 16.95 | 2543 | 779511 | 568404 | 2.27/2.59/5.50/6.59   |
| 3 | 16.78 | 2434 | 779511 | 568404 | 2.24/2.64/5.47/6.43   |
| 4 | 16.72 | 2557 | 779511 | 568404 | 2.27/2.63/5.50/6.32   |
| 5 | 16.66 | 2700 | 779511 | 568404 | 2.22/2.60/5.43/6.42   |

Only triples whose subject is an instance are kept as inferred (ontology-level closure and owl:Thing noise dropped, count in results.json).

### Correctness cross-check (2000 objects, 9086 edges)

- reasonable inferred 24988 (0.283 s); owlrl inferred 24988 (17.986 s), ignoring 2000 reflexive owl:sameAs triples that only owlrl emits
- reasonable == owlrl: **True** (reasonable-only 0, owlrl-only 0)
- reasonable == independent BFS transitive closure + inverse: **True** (missing 0, extra 0); owlrl == independent: True

## 3. SPARQL 4-hop (asserted graph; 40 random start nodes per variant; cold = first execution per node, warm = repeat)

Process-cold: the OS page cache is not dropped, so 'cold' = first touch per start node in a freshly opened store.

| run | variant | cold median / p95 / max s | warm median / p95 / max s | first query after open s | result size median / max |
|---|---|---|---|---|---|
| 1 | fixed_4hop | 0.0002 / 0.0004 / 0.0014 | 0.0002 / 0.0003 / 0.0004 | 0.0014 | 6.0 / 14 |
| 1 | plus_transitive | 0.0001 / 0.0002 / 0.0003 | 0.0001 / 0.0001 / 0.0001 | 0.0003 | 8.0 / 19 |
| 1 | mixed_4hop_shipment | 0.1551 / 0.1829 / 0.1872 | 0.1553 / 0.1674 / 0.1704 | 0.1829 | 56929.5 / 57000 |
| 1 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 2 | fixed_4hop | 0.0002 / 0.0004 / 0.0004 | 0.0002 / 0.0003 / 0.0004 | 0.0004 | 6.0 / 14 |
| 2 | plus_transitive | 0.0001 / 0.0001 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 2 | mixed_4hop_shipment | 0.1567 / 0.1768 / 0.1955 | 0.1561 / 0.1723 / 0.1755 | 0.1955 | 56929.5 / 57000 |
| 2 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 3 | fixed_4hop | 0.0002 / 0.0004 / 0.0004 | 0.0002 / 0.0003 / 0.0004 | 0.0004 | 6.0 / 14 |
| 3 | plus_transitive | 0.0001 / 0.0001 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 3 | mixed_4hop_shipment | 0.1552 / 0.1727 / 0.1798 | 0.1554 / 0.1650 / 0.1763 | 0.1797 | 56929.5 / 57000 |
| 3 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0000 | 0.0001 | 8.5 / 102 |
| 4 | fixed_4hop | 0.0002 / 0.0005 / 0.0005 | 0.0002 / 0.0003 / 0.0004 | 0.0005 | 6.0 / 14 |
| 4 | plus_transitive | 0.0001 / 0.0002 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 4 | mixed_4hop_shipment | 0.1571 / 0.1797 / 0.1852 | 0.1543 / 0.1670 / 0.1680 | 0.1846 | 56929.5 / 57000 |
| 4 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |
| 5 | fixed_4hop | 0.0002 / 0.0004 / 0.0004 | 0.0002 / 0.0003 / 0.0004 | 0.0004 | 6.0 / 14 |
| 5 | plus_transitive | 0.0001 / 0.0001 / 0.0002 | 0.0001 / 0.0001 / 0.0001 | 0.0001 | 8.0 / 19 |
| 5 | mixed_4hop_shipment | 0.1519 / 0.1834 / 0.1907 | 0.1511 / 0.1632 / 0.1677 | 0.1834 | 56929.5 / 57000 |
| 5 | inferred_lookup | 0.0000 / 0.0001 / 0.0001 | 0.0000 / 0.0000 / 0.0001 | 0.0001 | 8.5 / 102 |

## 4. pySHACL on a changed subgraph (330 random shipments + their ports/carriers, properties hydrated from Postgres)

| run | nodes | triples | clean s (conforms) | after deliberate violation s | detected (exactly the victim) |
|---|---|---|---|---|---|
| 1 | 903 | 5016 | 0.29 (True) | 0.22 | True |
| 2 | 903 | 5016 | 0.30 (True) | 0.21 | True |
| 3 | 903 | 5016 | 0.29 (True) | 0.22 | True |
| 4 | 903 | 5016 | 0.29 (True) | 0.22 | True |
| 5 | 903 | 5016 | 0.29 (True) | 0.21 | True |

## 5. 100 edits then resync + re-materialize

- 100 edits in Postgres: 0.31 s; outbox rows 100; resync 0.009 s (added 50, removed 25); full re-materialization 18.58 s; **total 18.59 s**; peak RSS 2519 MB. Re-materialization is a full recompute (no incremental path used).

## 6. Stability

5 consecutive runs (rebuild, materialize, queries, SHACL each in a fresh process). Materialization s: 17.28, 16.95, 16.78, 16.72, 16.66; peak RSS MB: 2152, 2543, 2434, 2557, 2700.

## Caveats

- Synthetic data; supplier networks are bounded (30 nodes) so transitive closure stays tractable; a real giant connected supply graph would blow up quadratically.
- The supplies 4-hop/`+` queries are cheap by construction (supplier networks of 30 nodes, results <= ~19 rows) and random starts almost never land on the 10 long chains; the mixed Shipment->Port->Shipment->Carrier->Shipment 4-hop (about 57k results) is the heavy one. Long-chain `+` latency was not specifically measured.
- Rebuild runs 2-5 are about 2x run 1 because rebuild first clears the existing asserted+inferred graphs (RocksDB clear_graph is slow: seconds per 1e5 quads). Run 1 hit a store with no inferred graph. Materialization writes only the delta of the inferred graph for the same reason.
- Re-materialization after edits is a full recompute (reasonable's incremental update_graph was not evaluated); at scale 1 it was slower than the first materialization because the delta diff reads the old inferred graph into Python.
- Inferred provenance is graph-level (inferred graph + derivedBy + dataVersion); per-edge rule provenance is not implemented (RDF-star unverified).
- The store holds ids/types/edges only; SHACL property shapes need properties hydrated from Postgres (done in the benchmark).
- Memory/time measured on one laptop with Docker running; page cache not dropped.
