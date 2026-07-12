# Security hardening

This document records the hardening applied to the ecosystem and the manual
steps only you can complete (key rotation).

## Do this first: rotate the exposed keys

The previous `.env.example` committed real, working provider keys. Treat all
four as compromised and rotate them. Rotating means: generate a new key in the
provider console, paste the new value into your local `.env`, then revoke the
old key.

1. DeepSeek — https://platform.deepseek.com/ — revoke the old key, issue a new one.
2. Anthropic — https://console.anthropic.com/ — revoke and reissue.
3. OpenAI — https://platform.openai.com/api-keys — revoke and reissue.
4. Google AI Studio — https://aistudio.google.com/app/apikey — revoke and reissue.

After rotating, check each provider's usage dashboard for charges you did not
make. The same four keys also sit in your live `.env`; update that file with the
new values so the stack keeps running.

## Generate the new local secrets

Add these to your `.env` (now git-ignored). On Windows use Git Bash or WSL for
`openssl`, or any 32+ character random string.

```
API_KEY=$(openssl rand -hex 32)
ADMIN_API_KEY=$(openssl rand -hex 32)
N8N_BASIC_AUTH_PASSWORD=$(openssl rand -base64 24)
SEARXNG_SECRET_KEY=$(openssl rand -hex 32)
```

`docker compose up` now fails fast if `N8N_BASIC_AUTH_PASSWORD` or
`SEARXNG_SECRET_KEY` is unset — that is intentional.

## What changed in code

API-key auth on every service. All nine FastAPI services (orchestrator, three
agents, indexer, retriever, notifier, video, whisper) now require a valid
`X-API-Key` header. Loopback (127.0.0.1) is trusted and skips the check, so the
local Command Centre and service-to-service calls keep working unchanged. Shared
logic lives in `common/security.py`.

Role gate on destructive and paid actions. `require_admin` protects cost reset,
memory mutation, uploads, harness runs, n8n tool calls, and the vault write
endpoints. Non-local callers need `ADMIN_API_KEY` for these; loopback is still
trusted.

Local-only binding. Services bind `127.0.0.1` by default via `HOST`. They are
unreachable from the network unless you deliberately set `HOST=0.0.0.0`, at
which point the API key becomes the gate.

CORS locked down. `allow_origins=["*"]` is replaced by an `ALLOWED_ORIGINS`
allowlist defaulting to the local cockpit origins.

Vault writes are audited, backed up, and previewable. `/productivity/capture`
and `/productivity/task/update` now: require admin, write an append-only line to
`logs/audit.jsonl`, snapshot the note to `logs/vault_backups/` before editing,
and accept `"dry_run": true` to return the exact change without writing.

Media path sandbox. The video and whisper services confine every input and
output path to `MEDIA_INPUT_ROOT` and the configured output directory before
FFmpeg runs. Paths outside both roots are rejected with HTTP 403.

Secret scanning. `.gitignore` excludes `.env`, logs, data, venvs, and backups.
`.gitleaks.toml` and `.pre-commit-config.yaml` add secret scanning.

## Turn on secret scanning

This repo has no git history yet. When you initialise it:

```
git init
pip install pre-commit gitleaks
pre-commit install
gitleaks detect --source . --config .gitleaks.toml   # full scan
```

## How the cockpit is affected

Nothing changes for normal local use. The cockpit talks to the orchestrator on
`localhost`, which is trusted. You only need to send `X-API-Key` if you reach a
service from another machine.
