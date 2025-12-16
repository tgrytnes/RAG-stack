import email
import email.policy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from typing import Dict, Tuple

import ocrmypdf  # noqa: F401 (ensures binary present)
import pdfplumber
import pytesseract
from PIL import Image

INBOX_DIR = os.environ.get("INBOX_DIR", "/app/inbox")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/app/archive")
STAGING_DIR = os.environ.get("STAGING_DIR", "/app/staging")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))

TEXT_EXTS = {".txt", ".md", ".rtf"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
PDF_EXTS = {".pdf"}
EMAIL_EXTS = {".eml"}


def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def file_checksum(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def ocr_pdf(src_path: str) -> Tuple[str, str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_out:
        tmp_out_path = tmp_out.name
    try:
        # Run OCR with sane defaults; optimize lightly to keep Pi load reasonable.
        subprocess.run(
            ["ocrmypdf", "--optimize", "1", "--rotate-pages", "--output-type", "pdf", src_path, tmp_out_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        text_parts = []
        with pdfplumber.open(tmp_out_path) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts), tmp_out_path
    finally:
        # Caller will delete tmp_out_path after use
        pass


def ocr_image(src_path: str) -> str:
    img = Image.open(src_path)
    return pytesseract.image_to_string(img)


def parse_email(src_path: str) -> Dict:
    with open(src_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    subject = msg.get("subject", "")
    sender = msg.get("from", "")
    to = msg.get("to", "")
    date = msg.get("date", "")
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body_parts.append(part.get_content().strip())
    else:
        if msg.get_content_type() == "text/plain":
            body_parts.append(msg.get_content().strip())
    body = "\n".join(body_parts)
    return {
        "subject": subject,
        "from": sender,
        "to": to,
        "date": date,
        "text": body,
    }


def extract_text(src_path: str, ext: str) -> Tuple[str, Dict]:
    meta: Dict = {}
    if ext in PDF_EXTS:
        text, tmp_pdf = ocr_pdf(src_path)
        meta["ocr_output"] = tmp_pdf
        return text, meta
    if ext in IMG_EXTS:
        return ocr_image(src_path), meta
    if ext in EMAIL_EXTS:
        mail_meta = parse_email(src_path)
        meta.update(mail_meta)
        return mail_meta.get("text", ""), meta
    if ext in TEXT_EXTS:
        with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(), meta
    # Fallback: try reading as text
    with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read(), meta


def build_sidecar(src_path: str, archive_path: str, item_type: str, text: str, meta: Dict) -> dict:
    checksum = file_checksum(src_path) if os.path.exists(src_path) else ""
    stable_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{archive_path}|{checksum}"))
    return {
        "id": stable_id,
        "source_path": src_path,
        "archived_path": archive_path,
        "item_type": item_type,
        "original_filename": os.path.basename(src_path),
        "checksum": checksum,
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "text": text,
        "metadata": meta,
    }


def process_file(src_path: str, rel_dir: str, filename: str) -> None:
    item_type = rel_dir.split(os.sep)[0] if rel_dir else "unknown"
    archive_subdir = os.path.join(ARCHIVE_DIR, rel_dir)
    ensure_dir(archive_subdir)
    ensure_dir(STAGING_DIR)

    archived_path = os.path.join(archive_subdir, filename)
    ext = os.path.splitext(filename.lower())[1]
    text, meta = extract_text(src_path, ext)
    sidecar = build_sidecar(src_path, archived_path, item_type, text, meta)
    sidecar_name = f"{os.path.splitext(filename)[0]}.json"
    sidecar_archive_path = os.path.join(archive_subdir, sidecar_name)
    sidecar_staging_path = os.path.join(STAGING_DIR, sidecar_name)

    shutil.move(src_path, archived_path)
    if meta.get("ocr_output") and os.path.exists(meta["ocr_output"]):
        os.remove(meta["ocr_output"])

    with open(sidecar_archive_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)

    shutil.copy2(sidecar_archive_path, sidecar_staging_path)
    print(f"[ETL] Archived {filename} -> {archived_path}, sidecar -> {sidecar_staging_path}")


def scan_once() -> None:
    for root, _, files in os.walk(INBOX_DIR):
        rel_dir = os.path.relpath(root, INBOX_DIR)
        if rel_dir == ".":
            rel_dir = ""
        for name in files:
            if name.startswith("."):
                continue
            src_path = os.path.join(root, name)
            try:
                process_file(src_path, rel_dir, name)
            except Exception as exc:  # noqa: BLE001
                print(f"[ETL] Failed to process {src_path}: {exc}")


def main() -> None:
    print(f"[ETL] Watching inbox {INBOX_DIR} -> archive {ARCHIVE_DIR}, staging {STAGING_DIR}")
    ensure_dir(INBOX_DIR)
    ensure_dir(ARCHIVE_DIR)
    ensure_dir(STAGING_DIR)
    while True:
        scan_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
