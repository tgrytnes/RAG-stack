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
- `etl` (Janitor, scaffolded): polls `01_inbox`, moves the original file to `03_archive`, writes a JSON sidecar (stub text/metadata) to `03_archive` and copies it into `.staging` for embedding.
- `embedder` (Librarian, scaffolded): on a loop, ingests JSONs from `.staging` into Weaviate with a deterministic toy embedding, then deletes the staging JSON; also watches `02_active` (`.md/.txt`) and re-embeds on change.

Run
---
```bash
cd ~/projects/RAG-stack
docker compose up -d --build
```
Weaviate: `http://<pi-ip>:8081` (GraphQL/REST endpoints).

Disaster recovery
-----------------
If Weaviate data corrupts: stop the stack, clear `.weaviate_data`, `docker compose up -d`, then run the embedder's "reindex from /app/archive" command/script to rebuild from JSON sidecars (add this command to the embedder image).
