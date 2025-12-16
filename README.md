Digital Vault stack (Weaviate + ETL + Embedder)
===============================================

Paths
-----
- Vault root: `/mnt/sda1/digital_vault`
  - `01_inbox` (with subfolders `documents/emails/notes/scans`): drop PDFs/images/emails to be processed.
  - `02_active` (with subfolders `documents/emails/notes/scans`): Obsidian/active docs watched for live embedding.
  - `03_archive` (with subfolders `documents/emails/notes/scans`): processed originals + JSON sidecars (long-term backup).
  - `.staging`: transient hand-off for JSONs headed to Weaviate.
  - `.weaviate_data`: Weaviate storage.
- Project root: `~/projects/RAG-stack` (this folder).

Compose services (docker-compose.yaml)
-------------------------------------
- `weaviate`: database, stores data in `/mnt/sda1/digital_vault/.weaviate_data` (no built-in vectorizer enabled).
- `etl` (Janitor, scaffolded): polls `01_inbox`, OCRs/extracts text (ocrmypdf+pdfplumber for PDFs, pytesseract for images, text read for md/txt, basic email parse for .eml), moves the original file to `03_archive`, writes a JSON sidecar with metadata/UUID/checksum to `03_archive` and copies it into `.staging` for embedding.
- `embedder` (Librarian, scaffolded): on a loop, ingests JSONs from `.staging` into Weaviate using Ollama embeddings (`EMBED_MODEL`, default `nomic-embed-text` at `http://host.docker.internal:11434`), then deletes the staging JSON; also watches `02_active` (`.md/.txt`) and re-embeds on change. Provides `--reindex` to reload all sidecars from archive. Requires the embedding model to be pulled in Ollama (e.g., `docker exec ollama ollama pull nomic-embed-text`). Exposes:
  - Search API on port 8001 -> POST `http://<pi-ip>:8001/search` with `{"query":"text","top_k":5}` returning hits (score, distance, host paths); health at `GET /health`.
  - OpenAI-compatible Chat API at `http://<pi-ip>:8001/v1` with model id `weaviate-search` (use in Open WebUI as a custom OpenAI endpoint). It performs vector search and summarizes hits via `CHAT_MODEL` (default `llama3.2`; pull a chat-capable model in Ollama and set `CHAT_MODEL` as needed).
  - File viewer: `GET http://<pi-ip>:8001/file?path=<container-path>` serves files under `/app/archive` and `/app/active_docs` (paths are included in search results as `*_view_url` / `*_host_path`).

Run
---
```bash
cd ~/projects/RAG-stack
docker compose up -d --build
```
Weaviate: `http://<pi-ip>:8081` (GraphQL/REST endpoints).

Reindex (disaster recovery)
---------------------------
If Weaviate data corrupts: stop the stack, clear `.weaviate_data`, `docker compose up -d`, then:
```bash
cd ~/projects/RAG-stack
docker compose exec embedder python app.py --reindex /app/archive
```
This reads all JSON sidecars from archive and re-ingests them without re-OCRing.

Disaster recovery
-----------------
If Weaviate data corrupts: stop the stack, clear `.weaviate_data`, `docker compose up -d`, then run the embedder's "reindex from /app/archive" command/script to rebuild from JSON sidecars (add this command to the embedder image).
