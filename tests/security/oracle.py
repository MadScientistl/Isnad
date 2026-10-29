"""Brute-force visibility oracle over FULL data, written independently of the SQL policies."""
from collections import deque

LINKS = {
    "admin": None,
    "planner": {"ships_via", "ships_from", "ships_to", "operated_by", "supplies", "affects"},
    "finance_analyst": {"invoices_for", "billed_by", "supplies", "ships_via", "ships_from", "ships_to",
                        "operated_by", "affects"},
    "regional_ops_lead": {"ships_via", "ships_from", "ships_to", "operated_by", "supplies", "affects"},
    "restricted": {"supplies", "ships_via", "operated_by", "ships_to"},
}
TYPES = {
    "admin": None,
    "planner": {"Shipment", "Carrier", "Supplier", "Port", "Warehouse", "Event", "PurchaseOrder"},
    "finance_analyst": {"Invoice", "PurchaseOrder", "Supplier", "Shipment", "Carrier", "Port", "Warehouse", "Event"},
    "regional_ops_lead": {"Port", "Warehouse", "Shipment", "Carrier", "Supplier", "Event"},
    "restricted": {"Supplier", "Shipment", "Port", "Carrier", "Warehouse"},
}


class Oracle:
    def __init__(self, types: dict, props: dict, links: list):
        self.types, self.props, self.links = types, props, links
        self.ships_to = {}
        for l, a, b in links:
            if l == "ships_to":
                self.ships_to.setdefault(a, set()).add(b)

    def visible(self, role, i):
        if i not in self.types:
            return False
        t = self.types[i]
        allowed = TYPES[role]
        if allowed is not None and t not in allowed:
            return False
        p = self.props[i]
        if role == "regional_ops_lead":
            if t in ("Port", "Warehouse"):
                return p.get("region") == "EMEA"
            if t == "Shipment":
                return any(self.types.get(w) == "Warehouse" and self.props[w].get("region") == "EMEA"
                           for w in self.ships_to.get(i, ()))
        if role == "restricted" and t == "Supplier":
            return p.get("country") is not None and p.get("country") != "XX"
        return True

    def version_visible(self, role, i, version_props):
        """D4 (strict): a historical version row is visible only if the object is CURRENTLY visible AND
        the role's row filter (when it has a simple property filter) also holds on the version itself."""
        if not self.visible(role, i):
            return False
        t = self.types[i]
        if role == "regional_ops_lead" and t in ("Port", "Warehouse"):
            return version_props.get("region") == "EMEA"
        if role == "restricted" and t == "Supplier":
            return version_props.get("country") is not None and version_props.get("country") != "XX"
        return True  # exists(...) filters depend on link state, identical for every version of the id

    def vis_ids(self, role):
        return {i for i in self.types if self.visible(role, i)}

    def vedges(self, role):
        ok = LINKS[role]
        return [(l, a, b) for l, a, b in self.links
                if (ok is None or l in ok) and self.visible(role, a) and self.visible(role, b)]

    def reach(self, role, c, depth):
        if not self.visible(role, c):
            return set()
        adj = {}
        for l, a, b in self.vedges(role):
            adj.setdefault(a, set()).add(b); adj.setdefault(b, set()).add(a)
        seen, fr = {c}, [c]
        for _ in range(depth):
            nxt = []
            for x in fr:
                for y in adj.get(x, ()):
                    if y not in seen:
                        seen.add(y); nxt.append(y)
            fr = nxt
        return seen

    def inferred(self, role):
        """Set of (src, link, dst) inferred edges visible for the role (supplies + supplied_by)."""
        sup = {}
        asserted = set()
        for l, a, b in self.vedges(role):
            if l == "supplies":
                sup.setdefault(a, set()).add(b); asserted.add((a, b))
        pairs = set()
        for s in sup:
            seen, st = set(), list(sup[s])
            while st:
                x = st.pop()
                if x in seen:
                    continue
                seen.add(x); st.extend(sup.get(x, ()))
            for d in seen:
                if d != s or True:
                    pairs.add((s, d))
        out = {(a, "supplies", b) for a, b in pairs if (a, b) not in asserted}
        out |= {(b, "supplied_by", a) for a, b in pairs}
        return out

    def shortest_paths_exist(self, role, a, b, hops):
        return b in self.reach(role, a, hops)
