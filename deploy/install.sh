#!/usr/bin/env bash
# =============================================================================
# Mini PC installer (Ubuntu Server / Debian)
# Sets up the whole stack as systemd services that start on boot and restart
# on failure. Run once as a user with sudo, from the repo root:
#
#   bash deploy/install.sh
#
# What it does:
#   1. Creates a Python venv at .venv and installs requirements.
#   2. Writes one systemd unit per Python service, plus the operating daemon
#      and the Telegram / email channels.
#   3. Installs the watchdog service + 5-minute timer.
#   4. Enables docker compose services with restart: unless-stopped.
#   5. Enables and starts everything.
#
# Re-running is safe: units are overwritten, services restarted.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
VENV="$REPO_DIR/.venv"
PY="$VENV/bin/python"
UNIT_DIR="/etc/systemd/system"

echo "[1/5] Python venv + dependencies"
if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

echo "[2/5] Docker stack"
if command -v docker >/dev/null 2>&1; then
  (cd "$REPO_DIR" && docker compose up -d)
else
  echo "  docker not found; install Docker Engine first (https://docs.docker.com/engine/install/)"
fi

echo "[3/5] systemd units"
# name : python -m module : description
SERVICES=(
  "rag-orchestrator|orchestrator.main|Orchestrator API + Command Centre (8000)"
  "rag-local-agent|agents.local_data_agent|Local data agent (8001)"
  "rag-search-agent|agents.search_agent|Search agent (8002)"
  "rag-cloud-agent|agents.cloud_agent|Cloud agent (8003)"
  "rag-notifier|notifications.notifier --serve|Apprise notifier (8004)"
  "rag-indexer|rag.indexer --serve|Vault indexer (8005)"
  "rag-retriever|rag.retriever|Hybrid retriever (8006)"
  "rag-daemon|orchestrator.daemon|Operating daemon (execution loop)"
  "rag-telegram|channels.telegram_bot|Telegram channel"
  "rag-email|channels.email_poller|Email channel"
)

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r name module desc <<<"$entry"
  sudo tee "$UNIT_DIR/$name.service" >/dev/null <<UNIT
[Unit]
Description=$desc
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$REPO_DIR/.env
ExecStart=$PY -m $module
Restart=on-failure
RestartSec=10
StandardOutput=append:$REPO_DIR/logs/$name.out.log
StandardError=append:$REPO_DIR/logs/$name.err.log

[Install]
WantedBy=multi-user.target
UNIT
done

# The daemon and channels depend on the orchestrator being up.
for name in rag-daemon rag-telegram rag-email; do
  sudo sed -i "s/^After=.*/After=network-online.target docker.service rag-orchestrator.service/" "$UNIT_DIR/$name.service"
done

echo "[4/5] watchdog"
sudo tee "$UNIT_DIR/rag-watchdog.service" >/dev/null <<UNIT
[Unit]
Description=RAG stack watchdog (deep health check, restarts failed services)

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/deploy/watchdog.sh
UNIT

sudo tee "$UNIT_DIR/rag-watchdog.timer" >/dev/null <<UNIT
[Unit]
Description=Run the RAG watchdog every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT

echo "[4b/5] weekly rehearsal timer"
sudo tee "$UNIT_DIR/rag-rehearsal.service" >/dev/null <<UNIT
[Unit]
Description=Weekly operational rehearsal (backup, restore dry run, rollback dry run)
After=rag-orchestrator.service

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$REPO_DIR/.env
ExecStart=/usr/bin/env bash $REPO_DIR/deploy/rehearsal.sh
UNIT

sudo tee "$UNIT_DIR/rag-rehearsal.timer" >/dev/null <<UNIT
[Unit]
Description=Run the operational rehearsal every Monday 05:30

[Timer]
OnCalendar=Mon *-*-* 05:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

echo "[5/5] enable + start"
mkdir -p "$REPO_DIR/logs"
sudo systemctl daemon-reload
for entry in "${SERVICES[@]}"; do
  IFS='|' read -r name _ _ <<<"$entry"
  # Telegram and email only start when their credentials exist in .env.
  if [ "$name" = "rag-telegram" ] && ! grep -qE '^(TELEGRAM_BOT_TOKEN|APPRISE_TELEGRAM_TOKEN)=..' "$REPO_DIR/.env" 2>/dev/null; then
    echo "  skipping $name (no Telegram token in .env)"; continue
  fi
  if [ "$name" = "rag-email" ] && ! grep -qE '^EMAIL_ALLOWED_SENDERS=..' "$REPO_DIR/.env" 2>/dev/null; then
    echo "  skipping $name (no EMAIL_ALLOWED_SENDERS in .env)"; continue
  fi
  sudo systemctl enable --now "$name.service"
done
sudo systemctl enable --now rag-watchdog.timer
sudo systemctl enable --now rag-rehearsal.timer

echo
echo "Done. Check: systemctl status rag-orchestrator; curl -s localhost:8000/health/deep"
