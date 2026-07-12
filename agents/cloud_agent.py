"""
Cloud Engine Agent — FastMCP Server
======================================
Verifies cloud storage status and retrieves infrastructure metadata.
Supports Google Cloud Storage (GCS) and AWS S3.

MCP Tools:
  • list_buckets(provider)              — list storage buckets
  • list_objects(bucket, prefix)        — list objects in a bucket/prefix
  • read_object(bucket, key)            — fetch text/JSON content of an object
  • retrieve(query, top_k)              — keyword search across object names

REST endpoint:
  POST /retrieve                         — for rag_node httpx calls

Run:
  python -m agents.cloud_agent
"""

from __future__ import annotations

import io
import json
import os
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

PORT: int = int(os.getenv("CLOUD_AGENT_PORT", "8003"))

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="cloud-engine-agent",
    instructions=(
        "Accesses cloud storage (GCS, S3) for documents and infrastructure metadata. "
        "Use `list_buckets` to discover available storage, `list_objects` to browse, "
        "and `read_object` to retrieve content."
    ),
)


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _gcs_client():
    try:
        from google.cloud import storage as gcs
        return gcs.Client()
    except Exception:
        return None


def _configured_gcs_buckets() -> list[str]:
    raw = os.getenv("GCS_BUCKET", "")
    return [b.strip() for b in raw.split(",") if b.strip()]


def _list_gcs_buckets() -> list[dict[str, Any]]:
    client = _gcs_client()
    if not client:
        return [{"error": "google-cloud-storage not installed or credentials missing"}]
    configured = _configured_gcs_buckets()
    if configured:
        return [{"name": name, "provider": "gcs", "configured": True} for name in configured]
    return [{"name": b.name, "location": b.location, "provider": "gcs"} for b in client.list_buckets()]


def _list_gcs_objects(bucket: str, prefix: str = "") -> list[dict[str, Any]]:
    client = _gcs_client()
    if not client:
        return []
    blobs = client.bucket(bucket).list_blobs(prefix=prefix, max_results=200)
    return [{"key": b.name, "size": b.size, "updated": str(b.updated), "bucket": bucket, "provider": "gcs"} for b in blobs]


def _read_gcs_object(bucket: str, key: str) -> str:
    client = _gcs_client()
    if not client:
        return "[Error] GCS client unavailable"
    blob = client.bucket(bucket).blob(key)
    data = blob.download_as_bytes()
    try:
        return data.decode("utf-8", errors="replace")[:8000]
    except Exception:
        return "[Binary content — not decodable as text]"


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_client():
    try:
        import boto3
        return boto3.client(
            "s3",
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
    except Exception:
        return None


def _list_s3_buckets() -> list[dict[str, Any]]:
    client = _s3_client()
    if not client:
        return [{"error": "boto3 not installed or credentials missing"}]
    resp = client.list_buckets()
    return [{"name": b["Name"], "created": str(b["CreationDate"]), "provider": "s3"} for b in resp.get("Buckets", [])]


def _list_s3_objects(bucket: str, prefix: str = "") -> list[dict[str, Any]]:
    client = _s3_client()
    if not client:
        return []
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=200)
    return [
        {"key": o["Key"], "size": o["Size"], "updated": str(o["LastModified"]), "bucket": bucket, "provider": "s3"}
        for o in resp.get("Contents", [])
    ]


def _read_s3_object(bucket: str, key: str) -> str:
    client = _s3_client()
    if not client:
        return "[Error] S3 client unavailable"
    obj = client.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    try:
        return data.decode("utf-8", errors="replace")[:8000]
    except Exception:
        return "[Binary content — not decodable as text]"


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_buckets(provider: Literal["gcs", "s3", "all"] = "all") -> list[dict[str, Any]]:
    """List storage buckets on the configured cloud provider(s)."""
    results = []
    if provider in ("gcs", "all"):
        results.extend(_list_gcs_buckets())
    if provider in ("s3", "all"):
        results.extend(_list_s3_buckets())
    return results


@mcp.tool()
def list_objects(bucket: str, prefix: str = "", provider: Literal["gcs", "s3"] = "gcs") -> list[dict[str, Any]]:
    """List objects/files inside a specific bucket with an optional key prefix."""
    if provider == "gcs":
        return _list_gcs_objects(bucket, prefix)
    return _list_s3_objects(bucket, prefix)


@mcp.tool()
def read_object(bucket: str, key: str, provider: Literal["gcs", "s3"] = "gcs") -> str:
    """Fetch and return the text content of a cloud storage object."""
    if provider == "gcs":
        return _read_gcs_object(bucket, key)
    return _read_s3_object(bucket, key)


@mcp.tool()
def retrieve(query: str, top_k: int = 5, provider: Literal["gcs", "s3", "all"] = "all") -> list[dict[str, Any]]:
    """
    Keyword search across all bucket object names.
    Returns objects whose key contains the query string (case-insensitive).
    """
    all_objects: list[dict[str, Any]] = []

    buckets = list_buckets(provider)
    for b in buckets:
        if "error" in b:
            continue
        name = b.get("name", "")
        prov = b.get("provider", "gcs")
        try:
            objs = list_objects(name, provider=prov)
            all_objects.extend(objs)
        except Exception:
            pass

    q = query.lower()
    matched = [o for o in all_objects if q in o.get("key", "").lower()]
    return matched[:top_k]


# ---------------------------------------------------------------------------
# REST wrapper
# ---------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
rest_app = FastAPI(title="Cloud Engine Agent REST Bridge", dependencies=[Depends(require_api_key)])
rest_app.add_middleware(CORSMiddleware, **cors_kwargs())


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5


@rest_app.post("/retrieve")
def rest_retrieve(req: RetrieveRequest):
    results = retrieve(req.query, req.top_k)
    chunks = [
        {
            "text": f"{r.get('bucket','')}/{r.get('key','')} ({r.get('size','')} bytes)",
            "provider": r.get("provider", "cloud"),
            "bucket": r.get("bucket", ""),
            "key": r.get("key", ""),
        }
        for r in results
    ]
    return {"chunks": chunks}


@rest_app.get("/health")
def health():
    return {"status": "ok", "agent": "cloud-engine-agent"}


# FastMCP renamed the ASGI factory across versions, so try each known name.
def _mcp_asgi(m):
    for name in ("http_app", "streamable_http_app", "sse_app", "get_asgi_app"):
        fn = getattr(m, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return None

_mcp_app = _mcp_asgi(mcp)
if _mcp_app is not None:
    rest_app.mount("/mcp", _mcp_app)
else:
    print("[cloud-agent] MCP ASGI app unavailable in this FastMCP version; REST endpoint still active.")


if __name__ == "__main__":
    uvicorn.run("agents.cloud_agent:rest_app", host=bind_host(), port=PORT, reload=False)
