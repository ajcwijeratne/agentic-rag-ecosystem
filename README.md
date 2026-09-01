# Agentic RAG Ecosystem

The versioned WijerCo workforce is in [`workforce/`](workforce/README.md): one orchestrator, 12 departments, 62 specialists and 118 generated role/capability skills. The implementation and institutional-readiness roadmap is in [`docs/ai-led-higher-education-institution-roadmap.md`](docs/ai-led-higher-education-institution-roadmap.md).

A fully autonomous agentic Retrieval-Augmented Generation system. A LangGraph orchestrator routes queries through a network of FastMCP sub-agents, retrieves context from local notes, live web search, and cloud storage, then synthesises answers using either a local Ollama model or DeepSeek-R1.

---

## Architecture

```
User Query
    │
    ▼
FastAPI /query (port 8000)
    │
    ▼
LangGraph Orchestrator
    ├── Route Node       → Pydantic router: local (Ollama) vs cloud (DeepSeek-R1)
    ├── RAG Node         → Calls all three FastMCP sub-agents in parallel
    │     ├── Local Data Agent  (port 8001) — Obsidian vault → Qdrant
    │     ├── Search Agent      (port 8002) — SearXNG / Tavily
    │     └── Cloud Agent       (port 8003) — GCS / S3
    ├── LLM Node         → Generates answer with retrieved context
    └── Synthesise Node  → Builds structured JSON output
            │
            ▼
    Apprise Notifier (port 8004) → Telegram · Email · Desktop

n8n (port 5678) — cron triggers → /webhook on orchestrator
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker + Docker Compose
- `ffmpeg` (for video pipeline): `brew install ffmpeg` / `choco install ffmpeg`

### 1. Clone and bootstrap

```bash
git clone <your-repo>
cd agentic-rag-ecosystem
bash scripts/setup.sh
```

This will:
- Create a Python virtual environment and install dependencies
- Copy `.env.example` → `.env`
- Start the Docker stack (Qdrant, Ollama, n8n, SearXNG)
- Pull `llama3` and `nomic-embed-text` into Ollama

### 2. Configure `.env`

Edit `.env` with your:
- `OBSIDIAN_VAULT_PATH` — path to your Obsidian vault
- `DEEPSEEK_API_KEY` — from [platform.deepseek.com](https://platform.deepseek.com)
- `APPRISE_TELEGRAM_TOKEN` + `APPRISE_TELEGRAM_CHAT_ID` — from [@BotFather](https://t.me/BotFather)
- Email SMTP credentials (optional)

### 3. Index your vault

```bash
source .venv/bin/activate
bash scripts/index_vault.sh
```

### 4. Start all services

```bash
bash scripts/start_all.sh
```

### 5. Run a query

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is agentic RAG and why does it matter?"}'
```

---

## Service Map

| Service | Port | Description |
|---|---|---|
| Orchestrator (FastAPI) | 8000 | Central command, `/query`, `/webhook` |
| Local Data Agent | 8001 | Obsidian vault + Qdrant retrieval |
| Search Agent | 8002 | SearXNG / Tavily web search |
| Cloud Agent | 8003 | GCS / S3 metadata + content |
| Notifier | 8004 | Apprise multi-channel notifications |
| Indexer | 8005 | Vault embedding + Qdrant upsert |
| Retriever | 8006 | Direct Qdrant semantic search |
| Whisper | 8007 | Audio → Markdown transcription |
| Voice Service | 8009 | VAD + Whisper + VOSK, REST and live WebSocket |
| Video Pipeline | 8008 | FFmpeg/MoviePy video ops |
| Qdrant | 6333 | Vector store (dashboard at /dashboard) |
| Ollama | 11434 | Local LLM inference |
| n8n | 5678 | Workflow automation |
| SearXNG | 8080 | Self-hosted web search |

---

## n8n Workflows

Import from `n8n/workflows/`:

- `rag_trigger.json` — runs a morning briefing query every weekday at 8am
- `vault_reindex.json` — re-indexes the Obsidian vault every Sunday at 2am

Go to n8n → Settings → Import Workflow.

---

## Model Routing Logic

The Pydantic router (`orchestrator/router.py`) selects the inference backend:

| Condition | Backend |
|---|---|
| Token count > 512 | DeepSeek-R1 (cloud) |
| Complexity keywords detected | DeepSeek-R1 (cloud) |
| Short / simple query | Ollama llama3 (local) |

Override thresholds via `ROUTER_TOKEN_THRESHOLD` in `.env`.

---

## Media Pipelines

### Audio → Transcript

```bash
python -m media.whisper_pipeline --file recording.mp3 --output ./transcripts
```

Or via REST:

```bash
curl -X POST http://localhost:8007/transcribe \
  -d '{"audio_path": "/path/to/file.mp3"}'
```

### Video operations

```bash
# Trim
python -m media.video_pipeline --trim input.mp4 00:01:00 00:02:30 output.mp4

# Extract audio for Whisper
curl -X POST http://localhost:8008/extract-audio \
  -d '{"input_path": "/path/to/video.mp4"}'
```

---

## Voice — VAD, Whisper and VOSK

Speech is a first-class input. Voice activity detection sits in front of both
recognisers, so silence never reaches a model: on a recording with long gaps
that is most of the runtime saved. The same gate runs on registry ingestion, so
every audio asset benefits, not just the live microphone.

### Engines

| Engine | Latency | Accuracy | Notes |
|---|---|---|---|
| `whisper` | utterance-level | highest | Punctuation, 90+ languages, auto-detect. Cannot produce partials. |
| `vosk` | tens of ms | good | True streaming, fully offline, one language per model. |
| `hybrid` | live + accurate | highest | VOSK drives the live caption; Whisper produces the text that is kept. |

On the same sentence, Whisper (`base`) returns `"What is Agentic Retrieval
Augmented Generation?"` while VOSK (small English model) returns `"what is agent
take retrieval augmented generation"` — faster and offline, but no punctuation
and weaker on unusual words. `hybrid` shows the VOSK draft while you speak and
keeps the Whisper text.

### VAD backends

`VAD_BACKEND=auto` picks the best installed and degrades rather than failing.

| Backend | Requires | Cost | Notes |
|---|---|---|---|
| `silero` | torch + `silero-vad` | ~35x realtime | Neural, best in noise. Default when installed. |
| `webrtc` | `webrtcvad` | ~1500x realtime | Very cheap, good in clean audio. |
| `energy` | numpy only | ~550x realtime | Adaptive noise-floor gate. Always available. |

Even Silero uses under 3% of one core, so any backend suits live streaming.
On Windows `webrtcvad` has no prebuilt wheel and needs MSVC, so
`requirements.txt` installs `webrtcvad-wheels` there — same import, same API.

### One-time VOSK setup

Models are not bundled (small English is ~70 MB) and are gitignored:

```powershell
.\scripts\download_vosk_model.ps1
```

Without a model, `vosk` and `hybrid` fall back to `whisper` automatically.

### Endpoints

Voice service (8009): `GET /engines`, `POST /vad`, `POST /transcribe`,
`POST /transcribe/upload`, `WS /ws/transcribe`.

Orchestrator (8000): `GET /voice/engines`, `POST /voice/transcribe`,
`POST /voice/ask` (audio in, routed answer out; operator role), `WS /voice/ws`.

Security matches every other service: loopback is trusted, remote callers need
`X-API-Key`, paths are confined to the media roots. Because a browser cannot set
headers on a WebSocket handshake, `require_api_key` also accepts `?api_key=` on
websocket connections.

### Live protocol

```
ws://localhost:8009/ws/transcribe

-> {"engine":"hybrid","sample_rate":48000,"vad_backend":"auto","silence_ms":600}
<- {"type":"ready","engine":"hybrid","vad":"silero","frame_ms":32}
-> <binary int16 mono PCM frames>
<- {"type":"speech_start","at_s":1.83}
<- {"type":"partial","text":"what is agentic","engine":"vosk"}
<- {"type":"final","text":"What is agentic RAG?","engine":"whisper","confidence":0.91}
<- {"type":"speech_end","at_s":4.20}
-> {"type":"stop"}
<- {"type":"transcript","text":"What is agentic RAG?","duration_s":5.1}
```

The Command Centre mic button uses the orchestrator socket and drops each
finished utterance into the composer, so a misheard question can be corrected
before it costs a model call. `getUserMedia` requires `https://` or `localhost`.

### Ingestion

`media/ingest/audio.py` transcribes through `media.asr` (VAD-gated, engine
selectable via `INGEST_ASR_ENGINE`), then writes segments to the registry's
`transcripts` table. If that layer cannot load it falls back to the original
`whisper_pipeline` path, so ingestion degrades rather than failing.

---

## Project Structure

```
agentic-rag-ecosystem/
├── docker-compose.yml          # Qdrant, Ollama, n8n, SearXNG
├── requirements.txt
├── .env.example
├── orchestrator/
│   ├── main.py                 # FastAPI entry point
│   ├── graph.py                # LangGraph state machine
│   ├── router.py               # Model routing logic
│   └── state.py                # Shared state schema
├── agents/
│   ├── local_data_agent.py     # FastMCP: Obsidian + Qdrant
│   ├── search_agent.py         # FastMCP: SearXNG / Tavily
│   └── cloud_agent.py          # FastMCP: GCS / S3
├── rag/
│   ├── embedder.py             # Ollama nomic-embed-text
│   ├── indexer.py              # Vault → Qdrant pipeline
│   └── retriever.py            # Semantic search service
├── media/
│   ├── audio.py                # Canonical PCM: decode, resample, framing
│   ├── vad.py                  # Voice activity detection (silero/webrtc/energy)
│   ├── asr.py                  # Unified ASR: whisper | vosk | hybrid + LiveSession
│   ├── vosk_engine.py          # VOSK offline streaming recogniser
│   ├── voice_service.py        # Voice service: REST + WebSocket (port 8009)
│   ├── ingest/audio.py         # Registry ingestion, VAD-gated
│   ├── whisper_pipeline.py     # Faster-Whisper transcription
│   └── video_pipeline.py       # FFmpeg / MoviePy
├── notifications/
│   └── notifier.py             # Apprise engine
├── n8n/workflows/              # n8n JSON workflow exports
├── config/
│   └── searxng-settings.yml
└── scripts/
    ├── setup.sh
    ├── start_all.sh
    └── index_vault.sh
```

---

## Phase Roadmap

| Phase | Weeks | Status |
|---|---|---|
| 1 — Docker + Orchestrator + FastAPI | 1–2 | Ready to deploy |
| 2 — Agentic RAG + Obsidian + n8n | 3–4 | Ready to deploy |
| 3 — Whisper + Video + Apprise | 5–6 | Ready to deploy |
