from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_dotenv(ROOT / ".env")

    try:
        from google.auth.exceptions import DefaultCredentialsError
        from google.cloud import storage
    except ImportError as exc:
        print(f"Missing dependency: {exc}")
        print("Install it with: .\\.venv\\Scripts\\python.exe -m pip install google-cloud-storage")
        return 1

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or None
    bucket_name = os.getenv("GCS_BUCKET", "").strip()
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    print(f"GOOGLE_CLOUD_PROJECT: {project or '<not set>'}")
    print(f"GOOGLE_APPLICATION_CREDENTIALS: {credentials_path or '<not set>'}")
    print(f"GCS_BUCKET: {bucket_name or '<not set>'}")

    if credentials_path and not Path(credentials_path).expanduser().exists():
        print("Credentials file does not exist at GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    try:
        client = storage.Client(project=project)
    except DefaultCredentialsError as exc:
        print(f"Credentials unavailable: {exc}")
        return 1

    if bucket_name:
        blobs = list(client.list_blobs(bucket_name, max_results=5))
        print(f"Bucket object listing accessible: {bucket_name}")
        print(f"Sample objects visible: {len(blobs)}")
        for blob in blobs:
            print(f"- {blob.name} ({blob.size} bytes)")
        return 0

    buckets = list(client.list_buckets(max_results=10))
    print(f"Buckets visible: {len(buckets)}")
    for bucket in buckets:
        print(f"- {bucket.name} ({bucket.location})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
