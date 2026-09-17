"""Vercel serverless entrypoint — exposes the Flask WSGI app as `app`.

Vercel gives each function instance a read-only filesystem with exactly one
writable directory: /tmp. This app writes on almost every request (audit_log
on each guarded view, post_mortems as a cache, chat_messages per turn), so the
committed database is copied to /tmp on cold start and VI_DB is pointed there.

Consequence, by design: that copy is per-instance and vanishes when the
instance is recycled. Reads are always correct and identical to local; the
audit trail and chat history are NOT durable and are not shared between
concurrent instances. Good enough for a demo, not for anything real — a
persistent deploy needs Postgres (see PORTING.md).
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "vendor_intelligence.db"
DST = Path("/tmp/vendor_intelligence.db")

if not DST.exists():
    shutil.copyfile(SRC, DST)

# Must be set BEFORE importing vi.*: both vi/db.py and vi/api.py read VI_DB at
# import time into module-level constants, so a later assignment is ignored.
os.environ["VI_DB"] = str(DST)

from vi.api import app  # noqa: E402

__all__ = ["app"]
