"""Library-level leak tests: direct ids, multi-hop, inferred paths, history, masking."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import PRINCIPALS, as_role, NOW  # noqa: E402
from oracle import Oracle  # noqa: E402
from engine.db import bitemporal as bt  # noqa: E402
from engine.sync import visibility as V  # noqa: E402

ROLES = ["regional_ops_lead", "planner", "finance_analyst", "restricted", "admin"]


@pytest.fixture(scope="module")
def orc(world):
    props = {k: dict(v) for k, v in world.props.items()}
    props[world["p_moved"]]["region"] = "APAC"
    return Oracle(world.types, props, world.links)


@pytest.mark.parametrize("role", ROLES)
def test_visible_ids_matches_oracle(world, orc, role):
    with as_role(world.schema, role) as c:
        got = V.visible_ids(c, list(world.types))
    assert got == orc.vis_ids(role)


@pytest.mark.parametrize("role", ROLES)
def test_visible_ids_ignores_unknown_ids(world, role):
    import uuid
    with as_role(world.schema, role) as c:
        assert V.visible_ids(c, [uuid.uuid4()]) == set()


def test_unknown_role_sees_nothing(world):
    from engine.db.principal import Principal
    with as_role(world.schema, "x", principal=Principal("nobody", "u", "")) as c:
        assert V.visible_ids(c, list(world.types)) == set()
        assert V.visible_edges(c, list(world.types)) == []


@pytest.mark.parametrize("name", ["p_apac", "p_amer", "w_apac", "s2", "inv1", "p_moved"])
def test_direct_hidden_center_is_not_visible(world, name):
    with as_role(world.schema, "regional_ops_lead") as c:
        with pytest.raises(V.NotVisible):
            V.bfs(c, world[name], 2, pipeline=world.pipe)
        assert V.node_infos(c, [world[name]]) == {}
        with pytest.raises(V.NotVisible):
            V.find_paths(c, world["s1"], world[name])


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("depth", [1, 2, 3, 4])
def test_bfs_nodes_match_oracle_every_depth(world, orc, role, depth):
    for cname in world.names:
        cid = world[cname]
        if not orc.visible(role, cid):
            continue
        with as_role(world.schema, role) as c:
            r = V.bfs(c, cid, depth, pipeline=world.pipe, max_nodes=1000)
        assert set(r.order) == orc.reach(role, cid, depth), (role, cname, depth)
        assert not r.truncated
        vis = orc.vis_ids(role)
        assert set(r.order) <= vis
        vedges = {(a, l, b) for l, a, b in orc.vedges(role)}
        assert set(r.edges) <= vedges
        assert {x for e in r.inferred for x in (e[0], e[2])} <= vis


def test_multihop_path_cut_at_hidden_middle(world):
    """s1 -(p_apac hidden)- s4: s4 is only reachable through a hidden port."""
    for depth in (1, 2, 3, 4):
        with as_role(world.schema, "regional_ops_lead") as c:
            r = V.bfs(c, world["s1"], depth, pipeline=world.pipe)
            assert world["p_apac"] not in r.order and world["s4"] not in r.order
            assert world["s2"] not in r.order
        with as_role(world.schema, "planner") as c:
            r = V.bfs(c, world["s1"], 2, pipeline=world.pipe)
            assert world["s4"] in r.order if depth >= 2 else True
    with as_role(world.schema, "regional_ops_lead") as c:
        assert V.find_paths(c, world["s1"], world["s4"], max_hops=4) == []
    with as_role(world.schema, "planner") as c:
        ps = V.find_paths(c, world["s1"], world["s4"], max_hops=4)
        assert ps and [n for n, _, _ in ps[0]] == [world["s1"], world["p_apac"], world["s4"]]


def test_find_paths_every_hop_visible(world, orc):
    for role in ROLES:
        vis = orc.vis_ids(role)
        with as_role(world.schema, role) as c:
            for a in world.names.values():
                for b in world.names.values():
                    if a == b or a not in vis or b not in vis:
                        continue
                    ps = V.find_paths(c, a, b, max_hops=4, max_paths=50)
                    for p in ps:
                        assert all(n in vis for n, _, _ in p)
                    assert bool(ps) == orc.shortest_paths_exist(role, a, b, 4)


def test_truncation_flags_do_not_leak(world):
    """Cut by cap only counts visible data: lead's view is identical whether or not hidden edges exist."""
    with as_role(world.schema, "regional_ops_lead") as c:
        r = V.bfs(c, world["car"], 2, pipeline=world.pipe, max_nodes=2)
        assert r.truncated and len(r.order) == 2
        r2 = V.bfs(c, world["car"], 1, pipeline=world.pipe, max_fanout=1)
        # carrier operates s1 and s2; s2 hidden => only s1 visible: fan-out 1 is NOT exceeded
        assert not r2.truncated and world["s2"] not in r2.order


def test_inverse_and_transitive_supplies_restricted(world):
    A, B, C, D, E, F, G, H = (world["sup_" + x] for x in "abcdefgh")
    with as_role(world.schema, "restricted") as c:
        r = V.bfs(c, A, 4, pipeline=world.pipe, max_nodes=1000)
    inf = set(r.inferred)
    assert B not in r.order and G not in r.order
    assert (A, "supplies", D) in inf            # via visible derivation A-E-D
    assert (D, "supplied_by", A) in inf
    assert (A, "supplies", C) not in inf        # only derivation goes through hidden B
    assert (C, "supplied_by", A) not in inf
    with as_role(world.schema, "restricted") as c:
        r = V.bfs(c, F, 4, pipeline=world.pipe)
        assert r.order == [F] and r.inferred == []        # F supplies G(hidden) supplies H: H unreachable
        r = V.bfs(c, H, 4, pipeline=world.pipe)
        assert r.order == [H] and r.inferred == []
    with as_role(world.schema, "admin") as c:
        r = V.bfs(c, F, 2, pipeline=world.pipe, is_admin=True)
    assert (F, "supplies", H) in set(r.inferred) and (H, "supplied_by", F) in set(r.inferred)


def test_inferred_visible_direct_probe(world):
    """Probing hidden inferred edges by name directly must not reveal them."""
    A, B, C, D = (world["sup_" + x] for x in "abcd")
    probe = [(A, "supplies", C), (A, "supplies", D), (C, "supplied_by", A), (A, "supplies", world["sup_h"])]
    with as_role(world.schema, "restricted") as c:
        assert V.inferred_visible(c, probe) == {(A, "supplies", D)}
        # bogus edge with no recorded derivation is hidden
        assert V.inferred_visible(c, [(A, "supplies", world["sup_f"])]) == set()
    with as_role(world.schema, "planner") as c:   # planner sees all suppliers
        assert V.inferred_visible(c, probe[:3]) == set(probe[:3])


def test_inferred_not_available_in_the_past(world):
    with as_role(world.schema, "planner") as c:
        r = V.bfs(c, world["sup_a"], 3, pipeline=world.pipe, as_of=NOW - __import__("datetime").timedelta(days=2))
    assert r.inferred == [] and not r.inferred_available


def test_history_follows_current_visibility(world):
    """p_moved was EMEA in the past, is APAC now: lead loses ALL versions (D4)."""
    from datetime import timedelta
    past = NOW - timedelta(days=3)
    pm = world["p_moved"]
    with as_role(world.schema, "regional_ops_lead") as c:
        assert V.visible_ids(c, [pm]) == set()
        assert V.visible_ids(c, [pm], as_of=past) == set()
        assert bt.get_object(c, "Port", pm, as_of_valid=past) is None
        assert bt.history(c, "Port", pm) == []
        for tbl in ("obj_port", "rd_obj_port", "cur_obj_port", "rd_cur_obj_port"):
            assert c.execute(f"SELECT count(*) FROM {tbl} WHERE id = %s", [pm]).fetchone()[0] == 0
        with pytest.raises(V.NotVisible):
            V.bfs(c, pm, 1, as_of=past)
        r = V.bfs(c, world["s1"], 2, as_of=past, pipeline=world.pipe)
        assert pm not in r.order and all(pm not in (e[0], e[2]) for e in r.edges)
    with as_role(world.schema, "planner") as c:
        assert len(bt.history(c, "Port", pm)) >= 2
        r = V.bfs(c, world["s1"], 1, as_of=past, pipeline=world.pipe)
        assert pm in r.order


def test_masked_properties_not_returned(world):
    with as_role(world.schema, "planner") as c:
        row = bt.get_object(c, "Supplier", world["sup_a"])
        assert row["country"] is None and row["name"] == "A"
        assert c.execute("SELECT country FROM rd_obj_supplier WHERE id=%s", [world["sup_a"]]).fetchone()[0] is None
    with as_role(world.schema, "finance_analyst") as c:
        assert bt.get_object(c, "Supplier", world["sup_a"])["country"] == "DE"


def test_node_infos_shape(world):
    with as_role(world.schema, "planner") as c:
        ni = V.node_infos(c, [world["s1"], world["inv1"]])
    assert set(ni) == {str(world["s1"])}
    assert ni[str(world["s1"])] == {"id": str(world["s1"]), "type": "Shipment", "label": "S1", "status": "in_transit"}


def test_finance_cannot_use_planner_only_links_nor_lead_hidden(world):
    with as_role(world.schema, "finance_analyst") as c:
        r = V.bfs(c, world["inv1"], 2, pipeline=world.pipe)
        assert world["sup_a"] in r.order and world["s1"] in r.order
    with as_role(world.schema, "planner") as c:
        with pytest.raises(V.NotVisible):
            V.bfs(c, world["inv1"], 1)


def test_inferred_support_table_does_not_expose_hidden_ids(world):
    hidden = {world["sup_b"], world["sup_g"]}
    with as_role(world.schema, "restricted") as c:
        rows = c.execute("SELECT src, dst, support FROM inferred_support").fetchall()
    seen = {x for r in rows for x in (r[0], r[1], *r[2])}
    assert not (seen & hidden)
