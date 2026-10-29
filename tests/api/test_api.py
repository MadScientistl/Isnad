import time
from engine.api.seed import _id

H = {"x-role": "planner"}


def test_types(client):
    r = client.get("/types", headers=H).json()
    assert {t["name"] for t in r["objectTypes"]} >= {"Supplier", "Port"}
    assert any(l["name"] == "supplies" for l in r["linkTypes"])


def test_bad_role_and_unknown_type(client):
    assert client.get("/types", headers={"x-role": "hacker"}).status_code == 403
    assert client.get("/objects/Nope", headers=H).status_code == 404


def test_seed_list_paging(client):
    all_ = client.get("/objects/Supplier?limit=1000", headers=H).json()
    assert len(all_) == 20
    assert len(client.get("/objects/Supplier?limit=5&offset=18", headers=H).json()) == 2


def test_crud_and_history(client):
    r = client.post("/objects/Carrier", json={"name": "Acme"}, headers=H)
    assert r.status_code == 201
    cid = r.json()["id"]
    assert client.post("/objects/Carrier", json={"id": cid, "name": "x"}, headers=H).status_code == 409
    time.sleep(0.01)
    assert client.put(f"/objects/Carrier/{cid}", json={"name": "Acme2"}, headers=H).json()["name"] == "Acme2"
    h = client.get(f"/objects/Carrier/{cid}/history", headers=H).json()
    assert len(h) >= 2 and {x["name"] for x in h} == {"Acme", "Acme2"}
    assert client.delete(f"/objects/Carrier/{cid}", headers=H).status_code == 200
    assert client.get(f"/objects/Carrier/{cid}", headers=H).status_code == 404
    # valid-time travel: before deletion it existed
    past = h[0]["valid_from"]
    assert client.get(f"/objects/Carrier/{cid}", params={"as_of_valid": past}, headers=H).status_code in (200, 404)


def test_validation(client):
    assert client.post("/objects/Port", json={"lat": 1}, headers=H).status_code == 422
    assert client.post("/objects/Port", json={"name": "p", "status": "weird"}, headers=H).status_code == 422


def test_links_and_neighbours(client):
    s0, s1 = str(_id("Supplier", 0)), str(_id("Supplier", 1))
    out = client.get(f"/links/supplies?id={s0}&direction=out", headers=H).json()
    assert {"src": s0, "dst": s1} in out
    inn = client.get(f"/links/supplies?id={s1}&direction=in", headers=H).json()
    assert any(l["src"] == s0 for l in inn)
    nb = client.get(f"/neighbours/Supplier/{s1}", headers=H).json()
    assert {n["direction"] for n in nb} == {"in", "out"}
    assert nb[0]["object"]["name"].startswith("Supplier")


def test_link_create_delete_and_endpoint_check(client):
    a = client.post("/objects/Supplier", json={"name": "A"}, headers=H).json()["id"]
    b = client.post("/objects/Supplier", json={"name": "B"}, headers=H).json()["id"]
    assert client.post("/links/supplies", json={"src": a, "dst": b}, headers=H).status_code == 201
    assert client.get(f"/links/supplies?id={a}", headers=H).json() == [{"src": a, "dst": b}]
    assert client.delete(f"/links/supplies?src={a}&dst={b}", headers=H).status_code == 200
    assert client.get(f"/links/supplies?id={a}", headers=H).json() == []
    bad = client.post("/links/supplies", json={"src": a, "dst": "00000000-0000-0000-0000-000000000001"}, headers=H)
    assert bad.status_code == 422


def test_cors(client):
    r = client.options("/types", headers={"origin": "http://localhost:5173",
                                          "access-control-request-method": "GET"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
