#!/usr/bin/env bash
# Trigger a full vault re-index.
# Usage: bash scripts/index_vault.sh [vault_path]

VAULT_PATH="${1:-$OBSIDIAN_VAULT_PATH}"
INDEXER_URL="${INDEXER_URL:-http://localhost:8005}"

echo "[index] Triggering vault index: $VAULT_PATH"
curl -s -X POST "$INDEXER_URL/index" \
  -H "Content-Type: application/json" \
  -d "{\"vault_path\": \"$VAULT_PATH\"}" | python3 -m json.tool
