#!/usr/bin/env bash
# =============================================================================
# Download a VOSK speech-recognition model.
#   bash scripts/download_vosk_model.sh
#   bash scripts/download_vosk_model.sh vosk-model-en-us-0.22
#
# Model list: https://alphacephei.com/vosk/models
#   vosk-model-small-en-us-0.15   ~40 MB   fast, good enough for live captions
#   vosk-model-en-us-0.22         ~1.8 GB  accurate, needs ~4 GB RAM
# =============================================================================
set -euo pipefail

MODEL_NAME="${1:-vosk-model-small-en-us-0.15}"

source .venv/bin/activate 2>/dev/null || true

echo "[vosk] Downloading model: $MODEL_NAME"
python -m media.vosk_engine --download --model-name "$MODEL_NAME"

echo ""
echo "[vosk] Verifying..."
python -m media.vosk_engine --status
