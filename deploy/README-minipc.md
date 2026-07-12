# Mini PC migration runbook

Moves the stack from the Windows PC to an always-on Linux box and makes it
self-booting. Pairs with EXTERNAL_SETUP.md (this is Mode A: everything bound
to localhost, reached over Tailscale). Allow one evening.

## 0. Hardware and OS

- Any mini PC with 16 GB RAM runs the stack with Ollama limited to
  nomic-embed-text (embeddings only). 32 GB lets llama3 keep serving local
  routing and summaries. SSD of 500 GB or more.
- Install Ubuntu Server 24.04 LTS. During install, enable OpenSSH.

## 1. Base software

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git curl ffmpeg
# Docker Engine + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # log out and back in
# Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up               # authenticate with your tailnet
```

## 2. Bring the repo and data across

On the mini PC:

```bash
git clone <your-repo-remote> ~/agentic-rag-ecosystem   # or copy the folder
cd ~/agentic-rag-ecosystem
```

Copy from the Windows PC (paths on the PC side):

| What | From (Windows) | To (mini PC) |
|---|---|---|
| Env + secrets | `.env` | repo root |
| Operating + media DB | `data/media.db` | `data/` |
| Qdrant vectors | Docker volume `qdrant_storage` | see below |
| Cost ledger + logs | `logs/*.jsonl` | `logs/` |

Qdrant volume: either re-index from scratch on the mini PC (clean, takes
minutes: `python -m rag.indexer --vault ... && python -m rag.indexer
--wijerco`) or export the Docker volume with `docker run --rm -v
qdrant_storage:/from -v $(pwd):/to alpine tar cf /to/qdrant.tar /from` and
unpack it on the target. Re-indexing is the recommended path; it also applies
the provenance fields everywhere.

## 3. Vault sync

The indexer needs the Obsidian vault on local disk. Install Syncthing on both
machines and share the vault folder. Do not use the OneDrive Linux clients
for this; placeholder hydration has already caused truncation problems on the
Windows side.

Set in `.env`:

```
OBSIDIAN_VAULT_PATH=/home/<you>/ObsidianVault
```

## 4. Env additions for the new components

Append to `.env` (see `.env.example` for the full block):

```
MONTHLY_BUDGET_USD=50          # 0 disables the breaker
DAEMON_INTERVAL_SEC=60
DAEMON_DRY_RUN=1               # start in dry-run; flip to 0 after first review
TELEGRAM_BOT_TOKEN=            # reuses APPRISE_TELEGRAM_TOKEN when empty
TELEGRAM_ALLOWED_CHAT_ID=      # reuses APPRISE_TELEGRAM_CHAT_ID when empty
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_USER=
EMAIL_PASS=
EMAIL_ALLOWED_SENDERS=a.j.c.wijeratne@gmail.com
```

## 5. Install services

```bash
bash deploy/install.sh
```

This creates the venv, starts Docker services, writes systemd units for the
seven core services plus the daemon and both channels, installs the watchdog
timer, and enables everything at boot. Telegram and email units are skipped
automatically until their credentials are in `.env`.

## 6. n8n

Open http://localhost:5678 (over Tailscale: http://<minipc-tailscale-ip>:5678
if you temporarily bind it, or use an SSH tunnel: `ssh -L 5678:localhost:5678
<minipc>`). Import the 13 workflows from `n8n-workflows/`, attach the SMTP
credential, set Header Auth on the MCP Server Trigger, put `N8N_MCP_TOKEN`
and `N8N_MCP_HEADER` in `.env`, restart the orchestrator
(`sudo systemctl restart rag-orchestrator`), and confirm `curl -s
localhost:8000/n8n/tools` lists the workflows. Then activate each workflow.

## 7. Acceptance test

```bash
# 1. Cold boot survival
sudo reboot
# wait 5 minutes, then from your laptop over Tailscale:
curl -s http://<minipc>:8000/health/deep     # via ssh tunnel or tailscale serve

# 2. Daemon liveness
curl -s localhost:8000/operating/daemon/status

# 3. End to end, still in dry run
curl -s -X POST localhost:8000/operating/plans/generate \
  -H 'Content-Type: application/json' \
  -d '{"goal":"Sector intelligence brief on TEQSA teaching qualification requirement","create":true}'
# watch logs/daemon.jsonl pick tasks up as dry_run entries

# 4. Go live
# set DAEMON_DRY_RUN=0 in .env, then:
sudo systemctl restart rag-daemon
```

## 8. Reaching the cockpit day to day

From any device on your tailnet, SSH tunnel once:
`ssh -L 8000:localhost:8000 <minipc>` then open
http://localhost:8000/app/command_centre.html. Or run `tailscale serve
https / http://localhost:8000` on the mini PC to publish it to your tailnet
only, TLS included.

## Rollback

The Windows install stays untouched. Stop the mini PC services
(`sudo systemctl stop 'rag-*'`), start the stack on the PC with the .bat, and
you are back where you were. Keep both alive for the first week; only one
daemon should have DAEMON_DRY_RUN=0 at a time.
