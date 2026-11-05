"""Graph comparison: change log entries and shrink detection."""
from __future__ import annotations

from typing import Any

from .model import Graph


def diff_graphs(old: Graph | None, new: Graph, sample: int = 200) -> dict[str, Any]:
    if old is None:
        return {"initial": True, "nodes_added": len(new.nodes), "nodes_removed": 0, "edges_added": len(new.edges),
                "edges_removed": 0, "added": [], "removed": [], "edges_added_sample": [], "edges_removed_sample": []}
    old_n, new_n = set(old.nodes), set(new.nodes)
    old_e = {e.key for e in old.edges}
    new_e = {e.key for e in new.edges}
    added = sorted(new_n - old_n)
    removed = sorted(old_n - new_n)
    e_add = sorted(new_e - old_e)
    e_rem = sorted(old_e - new_e)

    def lab(g: Graph, nid: str) -> dict[str, str]:
        n = g.nodes[nid]
        return {"id": nid, "label": n.label, "type": n.type}

    def elab(g: Graph, key: tuple[str, str, str]) -> str:
        return f"{g.nodes[key[0]].label} –{key[2]}→ {g.nodes[key[1]].label}"

    return {
        "initial": False,
        "nodes_added": len(added), "nodes_removed": len(removed),
        "edges_added": len(e_add), "edges_removed": len(e_rem),
        "added": [lab(new, n) for n in added[:sample]],
        "removed": [lab(old, n) for n in removed[:sample]],
        "edges_added_sample": [elab(new, k) for k in e_add[:sample]],
        "edges_removed_sample": [elab(old, k) for k in e_rem[:sample]],
    }


def shrink_check(old: Graph | None, new: Graph, ratio: float, type_ratio: float,
                 type_min: int) -> list[str]:
    """Return human-readable reasons if ``new`` lost a suspicious part of ``old``."""
    if old is None or not old.nodes:
        return []
    reasons = []
    o, n = len(old.nodes), len(new.nodes)
    if n < o * (1 - ratio):
        reasons.append(f"graph shrank from {o} to {n} nodes (-{(o - n) / o:.0%})")
    oc, nc = old.counts(), new.counts()
    for t, cnt in oc.items():
        if cnt >= type_min and nc.get(t, 0) < cnt * (1 - type_ratio):
            reasons.append(f"{t} nodes dropped from {cnt} to {nc.get(t, 0)}")
    return reasons
