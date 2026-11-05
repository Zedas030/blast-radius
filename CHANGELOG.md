# Changelog

## 1.1.0 – 2026-11-05

- New start page **"What if"**: pick what breaks from a grouped list sorted by impact (or a one-click scenario)
  and see every function as a green / amber / red tile with the chain of causes; failures can be combined.
- Web UI moved to the shared homelab theme (calm dark/light palette, light/dark toggle, cards and pills).
- Optional `ui.home_url` / `ui.home_label`: link back to your own dashboard.
- New endpoints `GET /api/catalog` and `GET /api/ui`.
- The map is built only when its tab is opened (faster first load).

## 1.0.0 – 2026-11-05

- First public release: discovery, dependency graph, impact simulation, SPOF list, cause hint, JSON API,
  nightly rediscovery, self-monitoring, Prometheus metrics.
