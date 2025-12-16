Embedder (Librarian)
====================
- Watch `/app/staging` for JSONs, embed content, send to Qdrant, then delete the staging JSON (backup lives in archive).
- Watch `/app/active_docs` for changes (Obsidian/notes) and re-embed on edits.
- Provide a command/script to scan `/app/archive` for all JSON sidecars to rebuild the index after a Qdrant wipe.
