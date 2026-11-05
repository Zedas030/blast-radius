"""Web UI + JSON API."""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__, metrics
from .analysis import resolve_question, search
from .config import load_config
from .engine import Engine

log = logging.getLogger("blast_radius")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def _loop(name: str, interval: float, fn: Any, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            fn()
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("%s loop failed", name)
        stop.wait(interval)


def create_app(cfg: dict[str, Any] | None = None, start_loops: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    engine = Engine(cfg)
    metrics.init(__version__)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        threads = []
        if start_loops:
            threads = [
                threading.Thread(target=_loop, args=("check", cfg["engine"]["check_interval_seconds"], engine.check, stop), daemon=True),
                threading.Thread(target=_loop, args=("ha", cfg["homeassistant"]["poll_seconds"], engine.poll_ha, stop), daemon=True),
            ]
            for t in threads:
                t.start()
        yield
        stop.set()

    app = FastAPI(title="Blast-Radius", version=__version__, lifespan=lifespan)
    app.state.engine = engine

    def graph_or_503() -> Any:
        try:
            return engine.require_graph()
        except LookupError:
            raise HTTPException(503, "No dependency graph yet – waiting for the first discovery.") from None

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": engine.graph is not None, "version": __version__, "alerts": len(engine.state.get("alerts", {}))}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return engine.status()

    @app.get("/api/graph")
    def graph(entities: bool = False) -> dict[str, Any]:
        g = graph_or_503()
        keep = {n.id for n in g.nodes.values() if entities or n.type != "entity"}
        return {
            "nodes": [n.to_dict() for n in g.nodes.values() if n.id in keep],
            "edges": [e.to_dict() for e in g.edges if e.src in keep and e.dst in keep],
            "counts": g.counts(), "meta": g.meta, "warnings": g.warnings,
        }

    @app.get("/api/node/{node_id:path}")
    def node(node_id: str) -> dict[str, Any]:
        g = graph_or_503()
        n = g.nodes.get(node_id)
        if n is None:
            raise HTTPException(404, f"unknown node {node_id}")

        def side(edges: list, attr: str) -> list[dict[str, Any]]:
            return [{"id": getattr(e, attr), "label": g.nodes[getattr(e, attr)].label, "type": g.nodes[getattr(e, attr)].type,
                     **e.to_dict()} for e in edges]
        return {**n.to_dict(), "depends_on": side(g.deps[node_id], "dst"), "dependents": side(g.rdeps[node_id], "src")}

    @app.get("/api/search")
    def api_search(q: str, limit: int = 20, entities: bool = True) -> list[dict[str, Any]]:
        return search(graph_or_503(), q, limit=limit, include_entities=entities)

    @app.get("/api/impact")
    def impact(node: list[str] = Query(default=[]), q: str = "", paths: bool = True) -> dict[str, Any]:
        g = graph_or_503()
        roots = [n for n in node if n in g.nodes]
        if q and not roots:
            hits = search(g, q, limit=1, include_entities=True)
            roots = [hits[0]["id"]] if hits else []
        if not roots:
            raise HTTPException(404, "no matching node")
        return engine.impact(roots, with_paths=paths)

    @app.post("/api/impact")
    def impact_post(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        g = graph_or_503()
        roots = [n for n in body.get("nodes", []) if n in g.nodes]
        if not roots:
            raise HTTPException(404, "no matching node")
        return engine.impact(roots, with_paths=bool(body.get("paths", True)))

    @app.get("/api/ask")
    def ask(q: str) -> dict[str, Any]:
        """Natural-language question for voice assistants, e.g. 'Was passiert, wenn der Pi abstürzt?'"""
        g = graph_or_503()
        hits = resolve_question(g, q)
        if not hits:
            return {"question": q, "matched": None, "alternatives": [],
                    "speech": "I could not tell which device or service you mean."}
        report = engine.impact([hits[0]["id"]], with_paths=False)
        return {"question": q, "matched": hits[0], "alternatives": hits[1:5], "speech": report["speech"],
                "summary": report["summary"], "functions": report["functions"], "counts": report["counts"]}

    @app.get("/api/spof")
    def api_spof() -> dict[str, Any]:
        graph_or_503()
        return engine.spof_cache or {}

    @app.get("/api/changes")
    def changes(limit: int = 100) -> list[dict[str, Any]]:
        return engine.changes(limit)

    @app.get("/api/live")
    def live() -> dict[str, Any]:
        return {**engine.live, "history": engine.state.get("causes", [])[:20]}

    @app.post("/api/cause/simulate")
    def cause_simulate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Explain a set of unavailable entities. Sends no notification unless "notify": true."""
        graph_or_503()
        ents = [str(e) for e in body.get("entities", [])]
        with engine.lock:
            finder = engine.finder
        result = finder.find(ents, ents) if finder else {}
        if body.get("notify") and result.get("candidates"):
            sent = engine.ha.notify("Blast-Radius (test)", "TEST: " + result["message"], cfg["web"].get("public_url", ""))
            result["notified"] = sent
        return result

    @app.post("/api/rebuild")
    def rebuild() -> dict[str, Any]:
        return engine.check(force=True)

    @app.post("/api/graph/accept")
    def accept() -> dict[str, Any]:
        engine.accept_pending()
        return engine.check(force=True)

    @app.get("/metrics", include_in_schema=False)
    def prom() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(LookupError)
    def lookup_handler(_req: Any, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    app = create_app(cfg)
    # first build right away so the UI is usable within seconds of a start
    try:
        app.state.engine.check(force=True)
    except Exception:  # noqa: BLE001
        log.exception("initial build failed")
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["web"]["port"]), log_level="warning")  # noqa: S104


if __name__ == "__main__":
    main()
