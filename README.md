Digital Vault stack (Qdrant + ETL + Embedder)
================================================

Paths
-----
- Vault root: `/mnt/sda1/digital_vault`
  - `01_inbox` (with subfolders `documents/emails/notes/scans`): drop PDFs/images/emails to be processed.
  - `02_active` (with subfolders `documents/emails/notes/scans`): Obsidian/active docs watched for live embedding.
  - `03_archive` (with subfolders `documents/emails/notes/scans`): processed originals + JSON sidecars (long-term backup).
  - `.staging`: transient hand-off for JSONs headed to Qdrant.
  - `.qdrant_data`: Qdrant storage.
- Project root: `~/projects/RAG-stack` (this folder).

Compose services (docker-compose.yaml)
-------------------------------------
- `qdrant`: database, stores data in `/mnt/sda1/digital_vault/.qdrant_data`.
- `etl` (Janitor): watch `01_inbox`, OCR/parse, create `file.json`, move PDF + JSON to `03_archive`, and drop JSON in `.staging`.
- `embedder` (Librarian): watch `.staging` + `02_active`, embed JSONs and notes, send to Qdrant, delete JSONs from `.staging`; can scan `03_archive` JSONs for disaster recovery.

Run
---
```bash
cd ~/projects/RAG-stack
docker compose up -d --build
```
Qdrant UI: `http://<pi-ip>:6333/dashboard`.

Disaster recovery
-----------------
If Qdrant data corrupts: stop the stack, clear `.qdrant_data`, `docker compose up -d`, then run the embedder's "reindex from /app/archive" command/script to rebuild from JSON sidecars (add this command to the embedder image).
