#!/usr/bin/env bash
# End-to-end test: builds the image, runs it with the anonymised sample homelab against a fake Home Assistant,
# and drives it through the API. Needs Docker and a Python with pytest + httpx (e.g. the dev venv).
# Usage: ./tests/e2e/run_e2e.sh [python]
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${1:-.venv/bin/python}"
WORK="$(mktemp -d /tmp/blast-radius-e2e.XXXXXX)"
NET=blast-radius-e2e
cleanup() {
  docker rm -f br-e2e-engine br-e2e-fakeha >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  # files written by the container user (uid 10001) are removed by that user
  [ -d "$WORK/state" ] && docker run --rm --user 10001 -v "$WORK/state:/w" python:3.13-slim sh -c 'rm -rf /w/*' >/dev/null 2>&1 || true
  rm -rf "$WORK"
  docker image rm blast-radius:e2e >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker rm -f br-e2e-engine br-e2e-fakeha >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true

mkdir -p "$WORK/facts" "$WORK/config" "$WORK/state"
"$PY" tests/sample_homelab.py "$WORK/facts" >/dev/null
mv "$WORK/facts/overrides.yaml" "$WORK/config/overrides.yaml"
cat > "$WORK/config/config.yaml" <<'EOF'
engine:
  facts_files: [/data/facts/*.json]
  overrides_file: /config/overrides.yaml
  data_dir: /data/state
  check_interval_seconds: 2
homeassistant:
  url: http://br-e2e-fakeha:8123
  notify_service: notify.mobile_app_test
  poll_seconds: 2
  startup_grace_seconds: 0
  mass_outage: {min_entities: 5, window_seconds: 60, cooldown_minutes: 60}
web:
  port: 8098
  public_url: http://127.0.0.1:18098
EOF
chmod -R a+rwX "$WORK"

docker build -q -t blast-radius:e2e . >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name br-e2e-fakeha --network "$NET" -p 127.0.0.1:18123:8123 \
  -v "$PWD/tests/e2e/fake_ha.py:/fake_ha.py:ro" blast-radius:e2e python /fake_ha.py >/dev/null
docker run -d --name br-e2e-engine --network "$NET" -p 127.0.0.1:18098:8098 \
  -e HA_TOKEN=e2e-token -v "$WORK/config:/config:ro" -v "$WORK/facts:/data/facts:ro" -v "$WORK/state:/data/state" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --memory 384m --cpus 0.5 \
  blast-radius:e2e >/dev/null

BR_FACTS="$WORK/facts/facts.json" "$PY" -m pytest -m e2e tests/e2e -q -p no:cacheprovider "${@:2}" \
  || { docker logs --tail 60 br-e2e-engine; exit 1; }
docker logs br-e2e-engine 2>&1 | tail -5
