"""Seeded synthetic supply-chain graph generator (fully deterministic for (seed, scale)).

scale 1.0 => ~100k objects, ~480k edges:
  Supplier 40000, Port 300, Warehouse 2000, Carrier 700, Shipment 57000
  operated_by 1/shipment, ships_via 5/shipment, supplies: 10 long chains (len 120) +
  suppliers grouped in 30-node supplier networks, each a random DAG (avg out-degree ~3.5),
  ~3% of networks get a back edge (cycle => whole network becomes one SCC).
Network size is bounded on purpose: transitive closure of one giant DAG would be ~N^2.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from uuid import UUID

NET = 30
CHAIN_LEN = 120
N_CHAINS = 10


@dataclass
class Dataset:
    objects: dict[str, list[tuple[UUID, dict]]] = field(default_factory=dict)
    links: dict[str, list[tuple[UUID, UUID]]] = field(default_factory=dict)
    cycles: int = 0

    @property
    def n_objects(self): return sum(len(v) for v in self.objects.values())
    @property
    def n_edges(self): return sum(len(v) for v in self.links.values())


def generate(seed: int = 1, scale: float = 1.0) -> Dataset:
    rnd = random.Random(seed)
    uid = lambda: UUID(int=rnd.getrandbits(128), version=4)
    n_sup = max(int(40000 * scale), 200)
    n_port = max(int(300 * scale), 6)
    n_wh = max(int(2000 * scale), 2)
    n_car = max(int(700 * scale), 3)
    n_ship = max(int(57000 * scale), 20)
    d = Dataset()
    sup = [uid() for _ in range(n_sup)]
    ports = [uid() for _ in range(n_port)]
    cars = [uid() for _ in range(n_car)]
    ships = [uid() for _ in range(n_ship)]
    d.objects["Supplier"] = [(i, {"name": f"Supplier {k}", "country": rnd.choice(["DE", "CN", "US", "IN", "BR"])})
                             for k, i in enumerate(sup)]
    d.objects["Port"] = [(i, {"name": f"Port {k}", "lat": rnd.uniform(-60, 60), "lon": rnd.uniform(-180, 180),
                              "status": rnd.choice(["open", "open", "open", "closed"])}) for k, i in enumerate(ports)]
    d.objects["Warehouse"] = [(uid(), {"name": f"WH {k}", "lat": rnd.uniform(-60, 60), "lon": rnd.uniform(-180, 180)})
                              for k in range(n_wh)]
    d.objects["Carrier"] = [(i, {"name": f"Carrier {k}"}) for k, i in enumerate(cars)]
    d.objects["Shipment"] = [(i, {"ref": f"SH-{k:07d}", "status": rnd.choice(["planned", "in_transit", "delivered"])})
                             for k, i in enumerate(ships)]
    sl = []
    pos = 0
    n_chains = min(N_CHAINS, n_sup // (CHAIN_LEN * 4))
    for _ in range(n_chains):  # long chains
        c = sup[pos:pos + CHAIN_LEN]; pos += CHAIN_LEN
        sl += list(zip(c, c[1:]))
    while pos + NET <= n_sup:  # supplier networks
        net = sup[pos:pos + NET]; pos += NET
        for a in range(NET):
            k = min(NET - a - 1, rnd.choice([2, 3, 3, 4, 4, 5]))
            for b in rnd.sample(range(a + 1, NET), k):
                sl.append((net[a], net[b]))
        if rnd.random() < 0.03 or d.cycles == 0 and pos >= 2 * NET:  # >=1 cycle at any scale
            sl.append((net[-1], net[0])); d.cycles += 1
    d.links["supplies"] = sl
    d.links["operated_by"] = [(s, rnd.choice(cars)) for s in ships]
    d.links["ships_via"] = [(s, p) for s in ships for p in rnd.sample(ports, 5)]
    return d
