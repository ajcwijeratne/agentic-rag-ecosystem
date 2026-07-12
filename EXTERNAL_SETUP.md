# Running the ecosystem on an external machine

This guide sets the stack up on a machine that is not your laptop, for example
a home server or a cloud VM. Read `SECURITY.md` alongside it. The short version:
on a remote box, loopback trust no longer covers you, so the API key and the
bind address now matter.

Pick one of two deployment modes before you start:

- Mode A, recommended. Bind every service to 127.0.0.1 on the server and reach
  the cockpit from your laptop over an SSH tunnel. Nothing is exposed to the
  network, everything works without juggling keys, and it is the least effort.
- Mode B, exposed. Bind to 0.0.0.0 and open ports. You then need the API key on
  every call, a reverse proxy with TLS in front, and a tight firewall. Only do
  this if a tunnel is not an option.

---

## 1. What you are deploying

Docker services: Qdrant (6333), Ollama (11434), n8n (5678), SearXNG (8080).

Python services, started from the repo:

| Service           | Port | Start command                          |
|-------------------|------|----------------------------------------|
| Orchestrator + UI | 8000 | `python -m orchestrator.main`          |
| Local data agent  | 8001 | `python -m agents.local_data_agent`    |
| Search agent      | 8002 | `python -m agents.search_agent`        |
| Cloud agent       | 8003 | `python -m agents.cloud_agent`         |
| Notifier          | 8004 | `python -m notifications.notifier --serve` |
| Indexer           | 8005 | `python -m rag.indexer --serve`        |
| Retriever         | 8006 | `python -m rag.retriever`              |
| Whisper (on demand)| 8007 | `python -m media.whisper_pipeline --serve` |
| Video (on demand) | 8008 | `python -m media.video_pipeline --serve`   |

The cockpit is served by the orchestrator at `/app/command_centre.html`.

---

## 2. Prerequisites

On the server:

- Docker and Docker Compose
- Python 3.11 or newer
- git
- ffmpeg, only if you use the video or whisper services

The services bind loopback by default. Internal calls between them use
`http://localhost:<port>`, so a single-box deployment needs no extra wiring.

---

## 3. Get the code

```bash
git clone <your-repo-url> agentic-rag-ecosystem
cd agentic-rag-ecosystem
```

---

## 4. Generate the secrets

You need four random secrets plus your provider keys.

Linux or macOS:

```bash
echo "API_KEY=$(openssl rand -hex 32)"
echo "ADMIN_API_KEY=$(openssl rand -hex 32)"
echo "SEARXNG_SECRET_KEY=$(openssl rand -hex 32)"
echo "N8N_BASIC_AUTH_PASSWORD=$(openssl rand -base64 24)"
```

Windows PowerShell:

```powershell
function New-Hex { $b = New-Object 'Byte[]' 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | % { $_.ToString('x2') }) -join '' }
function New-B64 { $b = New-Object 'Byte[]' 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b) }
"API_KEY=$(New-Hex)"; "ADMIN_API_KEY=$(New-Hex)"; "SEARXNG_SECRET_KEY=$(New-Hex)"; "N8N_BASIC_AUTH_PASSWORD=$(New-B64)"
```

Use fresh values, not the ones from any other machine.

---

## 5. Configure .env

```bash
cp .env.example .env
```

Then edit `.env`. The settings that matter on a remote box:

Required to start at all:

```
N8N_BASIC_AUTH_PASSWORD=<from step 4>
SEARXNG_SECRET_KEY=<from step 4>
```

Access control:

```
API_KEY=<from step 4>
ADMIN_API_KEY=<from step 4>
```

Provider keys, the ones you actually use:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
DEEPSEEK_API_KEY=...
```

Paths, point these at real locations on the server:

```
OBSIDIAN_VAULT_PATH=/srv/obsidian-vault
WIJERCO_PATH=/srv/wijerco
MEDIA_INPUT_ROOT=./media_input
VIDEO_OUTPUT_DIR=./video_output
TRANSCRIPT_OUTPUT_DIR=./transcripts
```

Binding and CORS depend on your mode:

Mode A, tunnel. Leave the defaults.

```
HOST=127.0.0.1
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
```

Mode B, exposed. Bind public and allow your real cockpit origin.

```
HOST=0.0.0.0
ALLOWED_ORIGINS=https://rag.example.com
```

---

## 6. Bring up the Docker stack

```bash
docker compose up -d
```

Compose now fails fast if `N8N_BASIC_AUTH_PASSWORD` or `SEARXNG_SECRET_KEY` is
empty. That is intended. Pull the Ollama models once it is up:

```bash
docker exec ollama ollama pull llama3
docker exec ollama ollama pull nomic-embed-text
```

---

## 7. Start the Python services

The bundled scripts create the venv, install dependencies, and launch everything.

Linux or macOS:

```bash
bash scripts/setup.sh        # first run only: venv, deps, docker, models
source .venv/bin/activate
bash scripts/start_all.sh
```

Windows:

```powershell
.\scripts\setup.ps1
.\scripts\start_all.ps1
```

`start_all` reads `.env`, so `HOST` and the keys apply to every service it
launches. Logs land in `./logs/`, one file per service.

---

## 8. Verify

On the server itself, loopback is trusted, so no key is needed:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/qdrant
```

To prove the key gate works, call from another machine. Without the header you
should get 401:

```bash
curl http://<server>:8000/cost                              # 401 expected
curl -H "X-API-Key: <API_KEY>" http://<server>:8000/cost    # 200
```

Admin actions need the admin key:

```bash
curl -X DELETE -H "X-API-Key: <ADMIN_API_KEY>" http://<server>:8000/cost
```

---

## 9. Reaching the cockpit

### Mode A: SSH tunnel (recommended)

Keep `HOST=127.0.0.1` on the server. From your laptop, forward the orchestrator
port:

```bash
ssh -L 8000:localhost:8000 you@server
```

Then open `http://localhost:8000/app/command_centre.html` in your browser. To
the server the request arrives as loopback, so it is trusted and the cockpit
works with no key and nothing is exposed to the network. This is the path to
prefer.

### Mode B: exposed

If you bind `0.0.0.0`, the browser cockpit reaches the server as a remote
caller, so its requests need the `X-API-Key` header. The shipped cockpit does
not send one, so do not expose it raw. Put a reverse proxy (Caddy, nginx,
Traefik) in front that terminates TLS and injects the header, restrict the
firewall to known source IPs, and never serve it over plain HTTP. Treat Mode B
as the advanced path and use the tunnel unless you have a specific reason not to.

---

## 10. Operating it

Stop everything:

```bash
bash scripts/stop_all.sh        # or .\scripts\stop_all.ps1
docker compose down
```

Logs:

```bash
tail -f logs/orchestrator.log
```

Security audit trail for vault writes:

```bash
tail -f logs/audit.jsonl
```

---

## 11. Hardening checklist for a remote box

- Rotate any provider keys that were ever committed. See `SECURITY.md`.
- Confirm `.env` is git-ignored. It is, by the shipped `.gitignore`.
- Keep `HOST=127.0.0.1` unless you have a proxy and firewall in place.
- Restrict the Docker ports too. By default Qdrant, Ollama, n8n, and SearXNG
  publish on all interfaces. On an exposed box, bind them to `127.0.0.1` in
  `docker-compose.yml` (for example `127.0.0.1:6333:6333`) or block them at the
  firewall, so only the orchestrator is reachable.
- Set a strong `N8N_BASIC_AUTH_PASSWORD`. n8n can run workflows.
- Back up `.env` somewhere safe. It now holds secrets that exist nowhere else.
```
