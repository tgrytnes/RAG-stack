import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests

STAGING_DIR = Path(os.environ.get("STAGING_DIR", "/app/staging"))
ACTIVE_DIR = Path(os.environ.get("ACTIVE_DIR", "/app/active_docs"))
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/app/archive"))
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://weaviate:8080")
CLASS_NAME = os.environ.get("WEAVIATE_CLASS", "Document")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
VECTOR_DIM = int(os.environ.get("VECTOR_DIM", "64"))


def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def deterministic_vector(text: str, dim: int) -> List[float]:
    data = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
    buf = (data * ((dim // len(data)) + 1))[:dim]
    return [b / 255.0 for b in buf]


def ensure_schema() -> None:
    desired_properties = [
        {"name": "text", "dataType": ["text"]},
        {"name": "item_type", "dataType": ["text"]},
        {"name": "source_path", "dataType": ["text"]},
        {"name": "archived_path", "dataType": ["text"]},
        {"name": "checksum", "dataType": ["text"]},
        {"name": "created_at", "dataType": ["text"]},
        {"name": "updated_at", "dataType": ["text"]},
    ]
    schema = {
        "class": CLASS_NAME,
        "vectorizer": "none",
    }
    existing = requests.get(f"{WEAVIATE_URL}/v1/schema")
    if existing.status_code == 200:
        classes = existing.json().get("classes", [])
        for c in classes:
            if c.get("class") == CLASS_NAME:
                # Add any missing properties one by one.
                existing_props = {p.get("name") for p in (c.get("properties") or [])}
                for prop in desired_properties:
                    if prop["name"] not in existing_props:
                        add = requests.post(
                            f"{WEAVIATE_URL}/v1/schema/{CLASS_NAME}/properties",
                            json=prop,
                        )
                        if add.status_code not in (200, 201):
                            raise RuntimeError(
                                f"Failed to add property {prop['name']}: {add.status_code} {add.text}"
                            )
                return
    # Class does not exist; create with properties.
    schema["properties"] = desired_properties
    resp = requests.post(f"{WEAVIATE_URL}/v1/schema", json=schema)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to ensure schema: {resp.status_code} {resp.text}; payload={json.dumps(schema)}"
        )


def upsert_object(obj_id: str, properties: Dict, vector: List[float]) -> None:
    payload = {
        "id": obj_id,
        "class": CLASS_NAME,
        "properties": properties,
        "vector": vector,
    }
    resp = requests.post(f"{WEAVIATE_URL}/v1/objects", json=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upsert object {obj_id}: {resp.status_code} {resp.text}")


def normalize_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except Exception:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw)))


def ingest_json(path: Path) -> None:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    text = data.get("text", "")
    obj_id = normalize_uuid(data.get("id") or str(path))
    props = {
        "text": text,
        "item_type": data.get("item_type", "unknown"),
        "source_path": data.get("source_path", ""),
        "archived_path": data.get("archived_path", ""),
        "checksum": data.get("checksum", ""),
        "created_at": data.get("created_at", iso_now()),
        "updated_at": iso_now(),
    }
    vector = deterministic_vector(text, VECTOR_DIM)
    upsert_object(obj_id, props, vector)


def process_staging_file(path: Path) -> None:
    ingest_json(path)
    path.unlink()
    print(f"[EMBEDDER] Ingested staging {path.name}")


def scan_staging() -> None:
    for path in STAGING_DIR.glob("*.json"):
        try:
            process_staging_file(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[EMBEDDER] Failed staging file {path}: {exc}")


def scan_active_files(state: Dict[str, float]) -> None:
    if not ACTIVE_DIR.exists():
        return
    for path in ACTIVE_DIR.rglob("*"):
        if path.is_dir() or path.suffix not in {".md", ".txt"}:
            continue
        mtime = path.stat().st_mtime
        key = str(path)
        if key in state and state[key] >= mtime:
            continue
        with path.open("r", encoding="utf-8") as f:
            text = f.read()
        obj_id = normalize_uuid(key)
        props = {
            "text": text,
            "item_type": "active",
            "source_path": key,
            "archived_path": "",
            "checksum": "",
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        vector = deterministic_vector(text, VECTOR_DIM)
        try:
            upsert_object(obj_id, props, vector)
            state[key] = mtime
            print(f"[EMBEDDER] Synced active file {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[EMBEDDER] Failed active file {path}: {exc}")


def reindex_archive(archive_dir: Path) -> None:
    count = 0
    for path in archive_dir.rglob("*.json"):
        try:
            ingest_json(path)
            count += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[EMBEDDER] Failed archive JSON {path}: {exc}")
    print(f"[EMBEDDER] Reindex complete. Loaded {count} JSON sidecars from archive.")


def main_loop() -> None:
    print(f"[EMBEDDER] Connecting to Weaviate at {WEAVIATE_URL}, class={CLASS_NAME}")
    ensure_schema()
    os.makedirs(STAGING_DIR, exist_ok=True)
    os.makedirs(ACTIVE_DIR, exist_ok=True)
    state: Dict[str, float] = {}
    while True:
        scan_staging()
        scan_active_files(state)
        time.sleep(POLL_INTERVAL)


def parse_args():
    parser = argparse.ArgumentParser(description="Embedder service for Digital Vault.")
    parser.add_argument(
        "--reindex",
        dest="reindex",
        nargs="?",
        const=str(ARCHIVE_DIR),
        help="Reindex all JSON sidecars from archive (default: /app/archive).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.reindex:
        ensure_schema()
        reindex_archive(Path(args.reindex))
        sys.exit(0)
    main_loop()
