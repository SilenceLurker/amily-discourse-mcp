"""SQLite is shared by independently spawned stdio servers, never a global active account."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

from .errors import AppError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def positive(value, name="id") -> int:
    if type(value) is not int or value <= 0:
        raise AppError(f"{name} must be a positive integer.")
    return value


def valid_origin(origin: str, *, allow_http=False) -> str:
    try:
        parts = urlsplit(origin)
        _ = parts.port
        if (parts.scheme not in (("https", "http") if allow_http else ("https",))
                or not parts.hostname or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or any(ord(char) < 33 or ord(char) > 126 for char in origin)
                or "\\" in origin):
            raise ValueError
        return f"{parts.scheme}://{parts.netloc.lower()}"
    except ValueError:
        raise AppError("Site must be an HTTPS origin, without credentials, path, query or fragment.") from None


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                    alias TEXT PRIMARY KEY, config TEXT NOT NULL, secret BLOB NOT NULL,
                    generation INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, revision INTEGER NOT NULL,
                    generation INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
                    status TEXT NOT NULL, approved_hash TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    request_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, result TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(draft_id, revision)
                );
            """)

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def add_account(self, alias: str, config: dict, encrypted: bytes):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", alias):
            raise AppError("Account alias must contain 1-64 ASCII letters, digits, underscores or hyphens.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", config["username"]):
            raise AppError("Invalid Discourse username.")
        if not config["categories"]:
            raise AppError("At least one allowed category is required.")
        for category in config["categories"]:
            positive(category, "category")
        if not set(config["actions"]) <= {"read", "create", "reply", "edit"} or not config["actions"]:
            raise AppError("Unknown or empty action policy.")
        with self.connect(write=True) as db:
            if db.execute("SELECT 1 FROM accounts WHERE alias=?", (alias,)).fetchone():
                raise AppError("Alias already exists. Rotate its key or create another alias.")
            db.execute("INSERT INTO accounts VALUES (?,?,?,1,1)", (alias, encode(config), encrypted))

    def account(self, alias: str, *, db=None, include_disabled=False) -> dict:
        if db is None:
            with self.connect() as connection:
                return self.account(alias, db=connection, include_disabled=include_disabled)
        row = db.execute("SELECT * FROM accounts WHERE alias=?", (alias,)).fetchone()
        if not row or (not row["enabled"] and not include_disabled):
            raise AppError("Account is unavailable.")
        return {**json.loads(row["config"]), "alias": alias, "generation": row["generation"],
                "enabled": bool(row["enabled"]), "secret": bytes(row["secret"])}

    def accounts(self) -> list[dict]:
        with self.connect() as db:
            return [{k: v for k, v in self.account(row[0], db=db, include_disabled=True).items() if k != "secret"}
                    for row in db.execute("SELECT alias FROM accounts ORDER BY alias").fetchall()]

    def rotate(self, alias: str, secret: bytes):
        with self.connect(write=True) as db:
            self.account(alias, db=db, include_disabled=True)
            db.execute("UPDATE accounts SET secret=?,generation=generation+1 WHERE alias=?", (secret, alias))

    def enable(self, alias: str, enabled: bool):
        with self.connect(write=True) as db:
            self.account(alias, db=db, include_disabled=True)
            db.execute("UPDATE accounts SET enabled=?,generation=generation+1 WHERE alias=?", (enabled, alias))

    def draft(self, alias: str, draft_id: str, *, db=None) -> dict:
        if db is None:
            with self.connect() as connection:
                return self.draft(alias, draft_id, db=connection)
        row = db.execute("SELECT * FROM drafts WHERE id=? AND account=?", (draft_id, alias)).fetchone()
        if not row:
            raise AppError("Draft is unavailable for this account.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["approved"] = result.pop("approved_hash") == result["hash"]
        return result

    def save(self, account: dict, payload: dict, draft_id: str | None, expected_revision: int | None) -> dict:
        with self.connect(write=True) as db:
            current_account = self.account(account["alias"], db=db)
            if current_account["generation"] != account["generation"]:
                raise AppError("Account changed; start again.")
            revision = 1
            if draft_id:
                previous = self.draft(account["alias"], draft_id, db=db)
                if previous["revision"] != expected_revision:
                    raise AppError("Draft revision conflict; read the current draft first.")
                if previous["status"] not in ("draft", "rejected"):
                    raise AppError("Published, in-flight or uncertain drafts cannot be edited. Inspect the result first.")
                revision = previous["revision"] + 1
            elif expected_revision is not None:
                raise AppError("expected_revision requires a draft_id.")
            draft_id = draft_id or str(uuid.uuid4())
            content_hash = digest({"account": account["alias"], "generation": account["generation"],
                                   "revision": revision, "payload": payload})
            db.execute("""INSERT INTO drafts VALUES (?,?,?,?,?,?, 'draft',NULL,?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,generation=excluded.generation,
                payload=excluded.payload,hash=excluded.hash,status='draft',approved_hash=NULL,updated_at=excluded.updated_at""",
                (draft_id, account["alias"], revision, account["generation"], encode(payload), content_hash, now()))
            return self.draft(account["alias"], draft_id, db=db)

    def approve(self, alias: str, draft_id: str, revision: int, content_hash: str):
        with self.connect(write=True) as db:
            account = self.account(alias, db=db)
            draft = self.draft(alias, draft_id, db=db)
            if (draft["revision"] != revision or draft["hash"] != content_hash
                    or draft["generation"] != account["generation"] or draft["status"] != "draft"):
                raise AppError("Approval target changed or is not a pending draft.")
            db.execute("UPDATE drafts SET approved_hash=hash WHERE id=?", (draft_id,))

    def operation(self, alias: str, draft_id: str, revision: int, request_id: str):
        with self.connect() as db:
            self.draft(alias, draft_id, db=db)
            row = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["draft_id"] != draft_id or row["revision"] != revision:
                    raise AppError("request_id is already bound to a different operation.")
                return {"status": row["status"], "result": json.loads(row["result"]) if row["result"] else None}
        return None

    def claim(self, alias: str, draft_id: str, revision: int, request_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
            raise AppError("request_id must contain 8-128 ASCII letters, digits, underscores or hyphens.")
        with self.connect(write=True) as db:
            account = self.account(alias, db=db)
            draft = self.draft(alias, draft_id, db=db)
            if draft["revision"] != revision or draft["generation"] != account["generation"]:
                raise AppError("Draft or account revision changed; prepare and review again.")
            if draft["status"] != "draft":
                raise AppError("This draft revision has already been submitted. Read its status; do not resend.")
            if account["require_approval"] and not draft["approved"]:
                raise AppError("Local approval of this exact draft revision is required.")
            try:
                db.execute("INSERT INTO operations VALUES (?,?,?,'in_flight',NULL,?)",
                           (request_id, draft_id, revision, now()))
            except sqlite3.IntegrityError:
                raise AppError("Operation already exists; query the original request_id.") from None
            db.execute("UPDATE drafts SET status='in_flight',updated_at=? WHERE id=?", (now(), draft_id))
            return draft

    def finish(self, alias: str, draft_id: str, revision: int, request_id: str, status: str, result: dict):
        with self.connect(write=True) as db:
            draft = self.draft(alias, draft_id, db=db)
            if draft["revision"] != revision:
                raise AppError("Draft revision changed during publication.")
            db.execute("UPDATE operations SET status=?,result=?,updated_at=? WHERE request_id=? AND draft_id=? AND revision=?",
                       (status, encode(result), now(), request_id, draft_id, revision))
            db.execute("UPDATE drafts SET status=?,updated_at=? WHERE id=?", (status, now(), draft_id))

    def pending_operation(self, alias: str, draft_id: str) -> dict:
        with self.connect() as db:
            draft = self.draft(alias, draft_id, db=db)
            row = db.execute("SELECT * FROM operations WHERE draft_id=? AND revision=?", (draft_id, draft["revision"])).fetchone()
            if not row or row["status"] not in ("unknown", "in_flight"):
                raise AppError("Only unknown or interrupted operations can be resolved locally.")
            return dict(row)
