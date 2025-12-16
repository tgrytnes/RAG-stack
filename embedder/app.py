import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

STAGING_DIR = Path(os.environ.get("STAGING_DIR", "/app/staging"))
ACTIVE_DIR = Path(os.environ.get("ACTIVE_DIR", "/app/active_docs"))
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/app/archive"))
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://weaviate:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "llama3.2")
CLASS_NAME = os.environ.get("WEAVIATE_CLASS", "Document")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
API_PORT = int(os.environ.get("EMBED_PORT", "8000"))


def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def embed_text(text: str) -> List[float]:
    payload = {"model": EMBED_MODEL, "input": text}
    resp = requests.post(f"{OLLAMA_URL}/api/embeddings", json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Embedding failed: {resp.status_code} {resp.text}")
    data = resp.json()
    vec = data.get("embedding")
    if not isinstance(vec, list):
        raise RuntimeError(f"Embedding response missing vector: {data}")
    return vec


def weaviate_search(vec: List[float], top_k: int) -> List[Dict]:
    gql = {
        "query": f"""
        {{
          Get {{
            {CLASS_NAME}(
              nearVector: {{vector: {json.dumps(vec)} }}
              limit: {top_k}
            ) {{
              _additional {{ score distance }}
              text
              item_type
              source_path
              archived_path
              checksum
              created_at
              updated_at
            }}
          }}
        }}
        """
    }
    resp = requests.post(f"{WEAVIATE_URL}/v1/graphql", json=gql)
    if resp.status_code != 200:
        raise RuntimeError(f"Weaviate error {resp.status_code}: {resp.text}")
    return resp.json().get("data", {}).get("Get", {}).get(CLASS_NAME, []) or []


def map_container_path(path: str) -> str:
    if not path:
        return ""
    mapping = [
        ("/app/active_docs", "/mnt/sda1/digital_vault/02_active"),
        ("/app/archive", "/mnt/sda1/digital_vault/03_archive"),
        ("/app/inbox", "/mnt/sda1/digital_vault/01_inbox"),
        ("/app/staging", "/mnt/sda1/digital_vault/.staging"),
    ]
    for prefix, host in mapping:
        if path.startswith(prefix):
            return path.replace(prefix, host, 1)
    return path


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
    resp = requests.put(f"{WEAVIATE_URL}/v1/objects/{obj_id}", json=payload)
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
    if not text:
        print(f"[EMBEDDER] Skipping empty text in {path}")
        return
    props = {
        "text": text,
        "item_type": data.get("item_type", "unknown"),
        "source_path": data.get("source_path", ""),
        "archived_path": data.get("archived_path", ""),
        "checksum": data.get("checksum", ""),
        "created_at": data.get("created_at", iso_now()),
        "updated_at": iso_now(),
    }
    vector = embed_text(text)
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
        vector = embed_text(text)
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

    app = FastAPI()

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    top_k: int = 5


@app.get("/health")
def health():
    return {"status": "ok", "model": EMBED_MODEL}


@app.post("/search")
def search(req: SearchRequest):
    try:
        vec = embed_text(req.query)
        hits = weaviate_search(vec, req.top_k)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    # Enrich hits with host paths and numeric scores.
    enriched = []
    for h in hits:
        add = h.get("_additional", {}) or {}
        enriched.append(
            {
                "score": add.get("score"),
                "distance": add.get("distance"),
                "item_type": h.get("item_type"),
                "source_path": h.get("source_path"),
                "source_host_path": map_container_path(h.get("source_path", "")),
                "archived_path": h.get("archived_path"),
                "archived_host_path": map_container_path(h.get("archived_path", "")),
                "text": h.get("text", ""),
                "created_at": h.get("created_at"),
                "updated_at": h.get("updated_at"),
            }
        )
    return {"results": enriched}


@app.get("/v1/models")
def list_models():
    return {"data": [{"id": "weaviate-search", "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    # Use the last user message as the query.
    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="No user message provided")
    query = user_msgs[-1].content
    try:
        vec = embed_text(query)
        hits_raw = weaviate_search(vec, req.top_k)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    # Enrich hits.
    hits = []
    for h in hits_raw:
        add = h.get("_additional", {}) or {}
        hits.append(
            {
                "score": add.get("score"),
                "distance": add.get("distance"),
                "item_type": h.get("item_type"),
                "source_path": h.get("source_path"),
                "source_host_path": map_container_path(h.get("source_path", "")),
                "archived_path": h.get("archived_path"),
                "archived_host_path": map_container_path(h.get("archived_path", "")),
                "text": h.get("text", ""),
                "created_at": h.get("created_at"),
                "updated_at": h.get("updated_at"),
            }
        )

    # Build a summary with an LLM if available.
    summary = "No results"
    if hits:
        try:
            bullet_lines = []
            for idx, h in enumerate(hits, 1):
                bullet_lines.append(
                    f"{idx}) score={h['score']} distance={h['distance']} type={h['item_type']} src={h['source_host_path'] or h['source_path']} text={h['text']}"
                )
            prompt = (
                "You are a helpful assistant summarizing vector search hits.\n"
                "Given the hits below, produce a short answer to the user's query and list each hit with score and host path.\n"
                f"User query: {query}\nHits:\n" + "\n".join(bullet_lines)
            )
            llm_payload = {
                "model": CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": "Summarize search hits concisely."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            }
            llm_resp = requests.post(f"{OLLAMA_URL}/api/chat", json=llm_payload, timeout=60)
            if llm_resp.status_code == 200:
                summary = llm_resp.json().get("message", {}).get("content", "") or "\n".join(bullet_lines)
            else:
                summary = "\n".join(bullet_lines)
        except Exception:
            summary = "\n".join(
                [
                    f"{idx}) score={h['score']} distance={h['distance']} type={h['item_type']} src={h['source_host_path'] or h['source_path']}"
                    for idx, h in enumerate(hits, 1)
                ]
            )

    content = summary
    now = int(time.time())
    return {
        "id": f"weaviate-search-{now}",
        "object": "chat.completion",
        "created": now,
        "model": "weaviate-search",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


stop_event = threading.Event()
ingestion_thread = threading.Thread(target=main_loop, daemon=True)
ingestion_thread.start()

uvicorn.run(app, host="0.0.0.0", port=API_PORT)
