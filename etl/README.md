ETL (Janitor)
==============
- Watch `/app/inbox` for new files (PDF/images/eml/etc).
- OCR/parse into text + metadata, emit `filename.json`.
- Move original file to `/app/archive`.
- Copy the JSON to `/app/archive` (backup) and move JSON to `/app/staging` for immediate indexing.
- Log actions; retry on failures. Delete from inbox only after success.
