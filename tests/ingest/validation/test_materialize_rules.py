"""Row materialisation rules: keys, duplicates, required, enums, links, stats."""
import pytest

from engine.ingest import materialize as M
from engine.ingest.sources import open_source

NS = "https://isnad.local/ws/t#"


def src(**files):
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    for n, t in files.items():
        (d / f"{n}.csv").write_text(t)
    return open_source("dir", d)


def col(c, tr="none", typ="string", required=False, **kw):
    return {"col": c, "transform": tr, "type": typ, "required": required, **kw}


def mapping(types, links=None):
    return {"version": 1, "namespace": NS, "profile_hash": "h", "types": types, "links": links or {}}


def test_identical_repeats_merge_conflicts_count():
    s = src(a="id,name,city\n1,Acme,Paris\n1,ACME , paris\n2,Bolt,Rome\n2,Bolt,Oslo\n3,Cog,X\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {
        "id": col("t1.c1"), "name": col("t1.c2"), "city": col("t1.c3")}}})
    st = M.new_stats()
    out = list(M.iter_objects(s, m, "A", stats=st))
    ts = st.types["A"]
    assert [p["id"] for _, p in out] == ["1", "2", "3"]
    assert ts.rows_read == 5 and ts.rows_merged == 1 and ts.duplicate_keys == 1 and ts.rows_loaded == 3
    assert out[1][1]["city"] == "Rome"            # first row wins
    assert ts.examples["duplicate_keys"]


def test_null_key_required_and_coercion_counts():
    s = src(a="id,qty,flag,when\n1,5,yes,2026-01-01\n,6,yes,2026-01-01\n3,x,maybe,not a date\n4,,,\nNA,1,no,2026-01-02\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {
        "id": col("t1.c1", typ="string"), "qty": col("t1.c2", "int", "int"),
        "flag": col("t1.c3", "bool", "bool"), "when": col("t1.c4", "timestamp:iso8601", "timestamp")}}})
    st = M.new_stats()
    out = dict((p["id"], p) for _, p in M.iter_objects(s, m, "A", stats=st))
    ts = st.types["A"]
    assert set(out) == {"1", "3", "4"} and ts.null_keys == 2
    assert "qty" not in out["3"] and "when" not in out["3"] and "flag" not in out["3"]   # failed values become null
    assert ts.props["qty"].coercion_failures == 1 and ts.props["flag"].coercion_failures == 1
    assert ts.props["when"].coercion_failures == 1
    assert ts.props["qty"].coercion_examples == ["x"]
    assert ts.props["qty"].nulls == 1


def test_required_null_drops_row_and_is_counted():
    s = src(a="id,name\n1,a\n2,\n3,c\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {
        "id": col("t1.c1", required=True), "name": col("t1.c2", required=True)}}})
    st = M.new_stats()
    assert len(list(M.iter_objects(s, m, "A", stats=st))) == 2
    assert st.types["A"].null_required == 1 and st.types["A"].rows_dropped == 1


def test_enum_checks_exact_trim_case_and_enum_map():
    s = src(a="id,st,kind\n1,Open,a\n2, closed ,b\n3,weird,c\n4,OPEN,zzz\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {
        "id": col("t1.c1"),
        "st": col("t1.c2", typ="enum", enum=["Open", "closed"]),
        "kind": col("t1.c3", 'enum_map:{"a":"alpha","B":"beta"}', "enum", enum=["alpha", "beta"])}}})
    st = M.new_stats()
    out = {p["id"]: p for _, p in M.iter_objects(s, m, "A", stats=st)}
    assert out["2"]["st"] == "closed" and out["4"]["st"] == "Open"       # trimmed / case-insensitive match
    assert out["1"]["kind"] == "alpha" and out["2"]["kind"] == "beta"     # enum_map key match is case-insensitive
    ps = st.types["A"].props
    assert ps["st"].enum_violations == 1 and ps["st"].enum_examples == ["weird"]
    assert ps["kind"].enum_violations == 2


def test_points_and_limit():
    s = src(a="id,lat,lon,g\n1,10,20,\n2,95,20,\n3,,,\n4,10,,\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {
        "id": col("t1.c1"), "loc": col(None, "point(t1.c2,t1.c3)", "point")}}})
    st = M.new_stats()
    out = {p["id"]: p for _, p in M.iter_objects(s, m, "A", stats=st)}
    assert out["1"]["loc"]["coordinates"] == [20.0, 10.0] and "loc" not in out["2"] and "loc" not in out["3"]
    assert st.types["A"].props["loc"].coercion_failures == 2         # out of range, and a lone half
    assert len(list(M.iter_objects(s, m, "A", limit=2, stats=M.new_stats()))) == 2


def test_fk_links_resolution_duplicates_and_null_fk():
    s = src(p="id,n\nP1,a\nP2,b\n", c="id,p\n1,P1\n2,P2\n3,P9\n4,\n5,P1\n")
    types = {"P": {"table": "t2", "key_cols": ["t2.c1"], "columns": {"id": col("t2.c1")}},
             "C": {"table": "t1", "key_cols": ["t1.c1"], "columns": {"id": col("t1.c1")}}}
    links = {"parent": {"kind": "fk", "from_type": "C", "to_type": "P", "from_col": "t1.c2", "to_type_key": "t2.c1",
                        "from_key_cols": ["t1.c1"], "table": "t1"}}
    # tables are ordered by file name: c.csv = t1, p.csv = t2
    m = mapping(types, links)
    st = M.new_stats()
    for t in ("P", "C"):
        list(M.iter_objects(s, m, t, stats=st))
    edges = list(M.iter_links(s, m, "parent", stats=st))
    ls = st.links["parent"]
    assert len(edges) == 3 and ls.unresolved_links == 1 and ls.dropped_links == 1 and ls.null_endpoints == 1
    assert ls.rows_read == 4 and round(ls.resolution, 2) == 0.75
    assert edges[0] == (M.object_id(NS, "C", ["1"]), M.object_id(NS, "P", ["P1"]))
    assert ls.examples


def test_join_table_links_and_identical_pairs_merge():
    s = src(a="id\n1\n2\n", b="id\nx\ny\n", j="a,b\n1,x\n1,x\n2,y\n2,zz\n")
    types = {"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {"id": col("t1.c1")}},
             "B": {"table": "t2", "key_cols": ["t2.c1"], "columns": {"id": col("t2.c1")}}}
    links = {"ab": {"kind": "join_table", "from_type": "A", "to_type": "B", "table": "t3", "src_col": "t3.c1",
                    "dst_col": "t3.c2", "src_key": "t1.c1", "dst_key": "t2.c1"}}
    m = mapping(types, links)
    st = M.new_stats()
    for t in ("A", "B"):
        list(M.iter_objects(s, m, t, stats=st))
    assert len(list(M.iter_links(s, m, "ab", stats=st))) == 2
    assert st.links["ab"].duplicate_links == 1 and st.links["ab"].unresolved_links == 1


def test_bad_mapping_raises_mapping_error_with_safe_message():
    s = src(a="id\n1\n")
    for cols in ({"id": col("t9.c1")}, {"id": col("t1.c7")}, {"id": col("t1.c1", "wat")}):
        m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": cols}})
        with pytest.raises(M.MappingError):
            list(M.iter_objects(s, m, "A"))
    with pytest.raises(M.MappingError):
        list(M.iter_objects(s, mapping({"A": {"table": "t5", "key_cols": ["t5.c1"], "columns": {}}}), "A"))
    with pytest.raises(M.MappingError):
        list(M.iter_objects(s, mapping({}), "Nope"))


def test_stats_to_dict_is_json_safe():
    import json
    s = src(a="id,n\n1,x\n1,y\n")
    m = mapping({"A": {"table": "t1", "key_cols": ["t1.c1"], "columns": {"id": col("t1.c1"), "n": col("t1.c2", "int", "int")}}})
    st = M.new_stats()
    list(M.iter_objects(s, m, "A", stats=st))
    json.dumps(st.to_dict())
