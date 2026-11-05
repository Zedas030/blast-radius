"""Command line: `blast-radius serve | build | impact <query> | ask <question> | spof | cause <entity ...>`."""
from __future__ import annotations

import argparse
import json
import sys

from .analysis import CauseFinder, impact_report, resolve_question, search, spof
from .build import build_graph
from .config import load_config, load_overrides


def _graph(cfg: dict):  # type: ignore[no-untyped-def]
    import glob

    facts = []
    for pattern in cfg["engine"]["facts_files"]:
        for p in sorted(glob.glob(pattern)):
            with open(p, encoding="utf-8") as fh:
                facts.append(json.load(fh))
    if not facts:
        sys.exit("no facts files found – run blast-radius-collect first")
    return build_graph(facts, load_overrides(cfg["engine"]["overrides_file"]), cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blast-radius")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web UI / API")
    sub.add_parser("build", help="build the graph from the facts and print counts + warnings")
    p = sub.add_parser("impact", help="what fails if <query> fails")
    p.add_argument("query", nargs="+")
    p = sub.add_parser("ask", help="natural-language question")
    p.add_argument("question", nargs="+")
    sub.add_parser("spof", help="single points of failure")
    p = sub.add_parser("cause", help="likely cause for unavailable entities")
    p.add_argument("entities", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        import os

        if args.config:
            os.environ["BLAST_RADIUS_CONFIG"] = args.config
        from .app import main as serve

        serve()
        return 0
    cfg = load_config(args.config)
    g = _graph(cfg)
    if args.cmd == "build":
        print(json.dumps({"counts": g.counts(), "edges": len(g.edges), "warnings": g.warnings}, indent=1, ensure_ascii=False))
    elif args.cmd in ("impact", "ask"):
        text = " ".join(args.query if args.cmd == "impact" else args.question)
        hits = search(g, text, limit=1) if args.cmd == "impact" else resolve_question(g, text)
        if not hits:
            print("no matching node")
            return 1
        r = impact_report(g, [hits[0]["id"]], with_paths=True)
        print(r["summary"])
        for f in r["functions"]["failed"]:
            print("  ✕", f["label"], "   ← " + " → ".join(s["label"] for s in f["path"]))
        for f in r["functions"]["degraded"]:
            print("  !", f["label"], "   ← " + " → ".join(s["label"] for s in f["path"]))
    elif args.cmd == "spof":
        r = spof(g, cfg["analysis"]["spof_threshold"])
        for x in r["spofs"]:
            same = f"  (same: {', '.join(m['label'] for m in x['same_impact'])})" if x["same_impact"] else ""
            print(f"{x['score']:.0%}  {x['label']}{same}")
    elif args.cmd == "cause":
        print(CauseFinder(g).find(args.entities, args.entities)["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
