#!/usr/bin/env bash
# Install or update Blast-Radius on this host. Run from the source directory:  sudo ./scripts/install.sh
# Idempotent: existing config.yaml, overrides.yaml and .env are never overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/blast-radius
DATA=/var/lib/blast-radius
UID_CONTAINER=10001

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "docker compose (v2) is required" >&2; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "python3-yaml is required for the collector: apt install python3-yaml" >&2; exit 1; }

echo "==> code to $DEST/app"
install -d -m 755 "$DEST" "$DEST/app" "$DEST/config"
rm -rf "$DEST/app.new"
mkdir -p "$DEST/app.new"
tar -C "$SRC" --exclude=./.venv --exclude=./local --exclude=./.git --exclude='*/__pycache__' -cf - . | tar -C "$DEST/app.new" -xf -
rm -rf "$DEST/app.old"
[ -d "$DEST/app" ] && mv "$DEST/app" "$DEST/app.old"
mv "$DEST/app.new" "$DEST/app"
rm -rf "$DEST/app.old"
install -m 644 "$SRC/docker-compose.yml" "$DEST/docker-compose.yml"

echo "==> configuration (kept if present)"
[ -f "$DEST/config/config.yaml" ] || install -m 644 "$SRC/config/config.example.yaml" "$DEST/config/config.yaml"
[ -f "$DEST/config/overrides.yaml" ] || install -m 644 "$SRC/config/overrides.example.yaml" "$DEST/config/overrides.yaml"
if [ ! -f "$DEST/.env" ]; then
  install -m 600 "$SRC/.env.example" "$DEST/.env"
  echo "    created $DEST/.env – enter HA_TOKEN there (see README, Secrets)"
fi
chmod 600 "$DEST/.env"; chown root:root "$DEST/.env"

echo "==> data directories"
install -d -m 755 "$DATA" "$DATA/facts"
install -d -m 755 -o "$UID_CONTAINER" -g "$UID_CONTAINER" "$DATA/state"

echo "==> collector"
cat > /usr/local/bin/blast-radius-collect <<EOF
#!/bin/sh
# Blast-Radius host collector (read-only). Config: $DEST/config/config.yaml
exec /usr/bin/python3 -I $DEST/app/blast_radius/collect.py --config $DEST/config/config.yaml "\$@"
EOF
chmod 755 /usr/local/bin/blast-radius-collect
install -m 644 "$SRC/systemd/blast-radius-collect.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/blast-radius-collect.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now blast-radius-collect.timer
systemctl start blast-radius-collect.service || true
systemctl --no-pager --lines=3 status blast-radius-collect.service || true

echo "==> container"
cd "$DEST"
docker compose build --pull
docker compose up -d
echo "==> done: http://$(hostname -I | awk '{print $1}'):8098"
