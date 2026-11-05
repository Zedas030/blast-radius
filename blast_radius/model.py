"""Typed dependency graph.

Every edge points from the *dependent* node to the node it depends on:

    entity:sensor.x --needs--> device:abc --controlled_by--> device:hub

Edge kinds:
    runs_on        software runs on a host / runtime
    needs          functional dependency
    powered_by     power supply
    controlled_by  a device is controlled through a hub / coordinator

Edge flags:
    soft   the dependent only degrades when this dependency fails; it fails when ALL its soft
           dependencies fail (e.g. a dashboard with many entities)
    group  hard alternatives: the dependent fails only when every edge of the same group failed
           (e.g. two DNS resolvers)
    weak   the dependent can only become degraded through this edge, never fail
           (e.g. a helper value that an automation writes keeps its last value)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

NODE_TYPES = (
    "hardware", "network", "cloud", "host", "service", "integration",
    "device", "entity", "automation", "script", "dashboard", "function",
)
EDGE_KINDS = ("runs_on", "needs", "powered_by", "controlled_by")

# How impact paths read: "<dependency> → (<label>) <dependent>", e.g. "Pi → (runs on it) Docker".
EDGE_LABELS = {
    "runs_on": "runs on it",
    "needs": "needs it",
    "powered_by": "powered by it",
    "controlled_by": "controlled via it",
}


@dataclass
class Node:
    id: str
    type: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    source: str = "discovered"  # discovered | override

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "label": self.label, "attrs": self.attrs,
                "aliases": self.aliases, "source": self.source}


@dataclass
class Edge:
    src: str
    dst: str
    kind: str = "needs"
    soft: bool = False
    group: str | None = None
    via: str = ""
    weak: bool = False  # where it was discovered (e.g. "env MQTT_HOST", "override")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.kind)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"src": self.src, "dst": self.dst, "kind": self.kind}
        if self.soft:
            d["soft"] = True
        if self.group:
            d["group"] = self.group
        if self.weak:
            d["weak"] = True
        if self.via:
            d["via"] = self.via
        return d


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}
        self.deps: dict[str, list[Edge]] = {}
        self.rdeps: dict[str, list[Edge]] = {}
        self.warnings: list[str] = []
        self.meta: dict[str, Any] = {}

    # ------------------------------------------------------------------ building
    def add_node(self, node_id: str, type_: str, label: str, *, attrs: dict | None = None,
                 aliases: Iterable[str] = (), source: str = "discovered") -> Node:
        if type_ not in NODE_TYPES:
            raise ValueError(f"unknown node type {type_!r} for {node_id}")
        node = self.nodes.get(node_id)
        if node is None:
            node = Node(node_id, type_, label, dict(attrs or {}), list(aliases), source)
            self.nodes[node_id] = node
            self.deps.setdefault(node_id, [])
            self.rdeps.setdefault(node_id, [])
        else:
            if attrs:
                node.attrs.update(attrs)
            for a in aliases:
                if a not in node.aliases:
                    node.aliases.append(a)
        return node

    def add_edge(self, src: str, dst: str, kind: str = "needs", *, soft: bool = False,
                 group: str | None = None, via: str = "", weak: bool = False) -> Edge | None:
        if kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind {kind!r}")
        if src == dst or src not in self.nodes or dst not in self.nodes:
            return None
        key = (src, dst, kind)
        existing = self._edges.get(key)
        if existing is not None:
            # a hard dependency wins over a soft one
            if existing.soft and not soft:
                existing.soft = False
            if existing.weak and not weak:
                existing.weak = False
            return existing
        edge = Edge(src, dst, kind, soft and not weak, group, via, weak)
        self._edges[key] = edge
        self.deps[src].append(edge)
        self.rdeps[dst].append(edge)
        return edge

    def remove_edge(self, src: str, dst: str, kind: str | None = None) -> int:
        keys = [k for k in self._edges if k[0] == src and k[1] == dst and (kind is None or k[2] == kind)]
        for k in keys:
            e = self._edges.pop(k)
            self.deps[src].remove(e)
            self.rdeps[dst].remove(e)
        return len(keys)

    def remove_node(self, node_id: str) -> None:
        for e in list(self.deps.get(node_id, [])) + list(self.rdeps.get(node_id, [])):
            self.remove_edge(e.src, e.dst, e.kind)
        self.nodes.pop(node_id, None)
        self.deps.pop(node_id, None)
        self.rdeps.pop(node_id, None)

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def has(self, node_id: str) -> bool:
        return node_id in self.nodes

    def of_type(self, *types: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.type in types]

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [e.to_dict() for e in sorted(self._edges.values(), key=lambda e: e.key)],
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Graph":
        g = cls()
        g.meta = dict(data.get("meta") or {})
        g.warnings = list(data.get("warnings") or [])
        for n in data.get("nodes", []):
            g.add_node(n["id"], n["type"], n["label"], attrs=n.get("attrs"), aliases=n.get("aliases", []),
                       source=n.get("source", "discovered"))
        for e in data.get("edges", []):
            g.add_edge(e["src"], e["dst"], e.get("kind", "needs"), soft=e.get("soft", False),
                       group=e.get("group"), via=e.get("via", ""), weak=e.get("weak", False))
        return g

    def counts(self) -> dict[str, int]:
        out = {t: 0 for t in NODE_TYPES}
        for n in self.nodes.values():
            out[n.type] += 1
        return out
