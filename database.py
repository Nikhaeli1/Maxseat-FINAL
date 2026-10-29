"""
database.py — Persistent storage for MaxSeat Alert System
Uses PostgreSQL (Neon on Vercel) via the DATABASE_URL env var (POSTGRES_URL also accepted).
Falls back to SQLite for local development when no database URL is set.

On Vercel every request can land on a different short-lived server, so this database is the
only shared state: users, vehicles (including the latest sensor reading), logs, login tokens
and profile pictures all live here.
"""

import os, json, time
from sqlalchemy import create_engine, Column, Integer, String, Text, LargeBinary, Float
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

ON_VERCEL = bool(os.environ.get("VERCEL"))

# ── Connection setup ────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""

# Some hosts give postgres:// — SQLAlchemy 1.4+ needs postgresql+psycopg2://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# Local dev fallback: SQLite file in the project root.
# On Vercel the project folder is read-only, so the fallback goes to /tmp — that data is
# wiped often and is NOT shared between servers. Always set DATABASE_URL on Vercel.
if not DATABASE_URL:
    if ON_VERCEL:
        print("[DB] WARNING: DATABASE_URL is not set — using a temporary SQLite file. Data will be lost.")
        DATABASE_URL = "sqlite:////tmp/maxseat.db"
    else:
        DATABASE_URL = "sqlite:///./maxseat.db"

# Serverless functions are frozen between requests, so pooled connections go stale.
# NullPool opens a fresh connection per use (use Neon's *pooled* connection string).
if ON_VERCEL and not DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ── ORM models (all data stored as JSON text for schema flexibility) ────────
class DBUser(Base):
    __tablename__ = "ms_users"
    username = Column(String, primary_key=True, index=True)
    data     = Column(Text, nullable=False)


class DBPuv(Base):
    __tablename__ = "ms_puvs"
    id   = Column(Integer, primary_key=True)
    data = Column(Text, nullable=False)


class DBAuditLog(Base):
    __tablename__ = "ms_audit_logs"
    id   = Column(Integer, primary_key=True, autoincrement=True)
    data = Column(Text, nullable=False)


class DBCitation(Base):
    __tablename__ = "ms_citations"
    id   = Column(Integer, primary_key=True, autoincrement=True)
    data = Column(Text, nullable=False)


class DBComplaint(Base):
    __tablename__ = "ms_complaints"
    id   = Column(Integer, primary_key=True, autoincrement=True)
    data = Column(Text, nullable=False)


class DBToken(Base):
    """Mobile app login tokens (enforcer and passenger). Kept here, not in memory,
    so a token issued by one server is accepted by every other server."""
    __tablename__ = "ms_tokens"
    token   = Column(String, primary_key=True)
    kind    = Column(String, index=True, nullable=False)
    data    = Column(Text, nullable=False)
    created = Column(Float, nullable=False)


class DBAvatar(Base):
    """Profile pictures. Vercel can't save uploaded files to disk, so images go here."""
    __tablename__ = "ms_avatars"
    username     = Column(String, primary_key=True)
    content_type = Column(String, nullable=False)
    image        = Column(LargeBinary, nullable=False)


# ── Schema creation ─────────────────────────────────────────────────────────
def init_db():
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(engine)


# ── Users ───────────────────────────────────────────────────────────────────
def db_save_user(username: str, data: dict):
    try:
        with SessionLocal() as session:
            existing = session.get(DBUser, username)
            if existing:
                existing.data = json.dumps(data)
            else:
                session.add(DBUser(username=username, data=json.dumps(data)))
            session.commit()
    except Exception as e:
        print(f"[DB] db_save_user error: {e}")


def db_delete_user(username: str):
    try:
        with SessionLocal() as session:
            user = session.get(DBUser, username)
            if user:
                session.delete(user)
                session.commit()
    except Exception as e:
        print(f"[DB] db_delete_user error: {e}")


def db_load_users() -> dict:
    try:
        with SessionLocal() as session:
            rows = session.query(DBUser).all()
            return {row.username: json.loads(row.data) for row in rows}
    except Exception as e:
        print(f"[DB] db_load_users error: {e}")
        return {}


# ── PUVs ────────────────────────────────────────────────────────────────────
def db_save_puv(puv_id: int, data: dict):
    try:
        with SessionLocal() as session:
            existing = session.get(DBPuv, puv_id)
            if existing:
                existing.data = json.dumps(data)
            else:
                session.add(DBPuv(id=puv_id, data=json.dumps(data)))
            session.commit()
    except Exception as e:
        print(f"[DB] db_save_puv error: {e}")


def db_delete_puv(puv_id: int):
    try:
        with SessionLocal() as session:
            puv = session.get(DBPuv, puv_id)
            if puv:
                session.delete(puv)
                session.commit()
    except Exception as e:
        print(f"[DB] db_delete_puv error: {e}")


def db_load_puvs() -> list:
    try:
        with SessionLocal() as session:
            rows = session.query(DBPuv).all()
            return [json.loads(row.data) for row in rows]
    except Exception as e:
        print(f"[DB] db_load_puvs error: {e}")
        return []


# ── Audit logs ───────────────────────────────────────────────────────────────
def db_append_audit(data: dict):
    try:
        with SessionLocal() as session:
            session.add(DBAuditLog(data=json.dumps(data)))
            session.commit()
    except Exception as e:
        print(f"[DB] db_append_audit error: {e}")


def db_load_audit_logs(limit: int = 1000) -> list:
    """Newest first. Capped so the log doesn't slow every page load as it grows."""
    try:
        with SessionLocal() as session:
            rows = session.query(DBAuditLog).order_by(DBAuditLog.id.desc()).limit(limit).all()
            return [json.loads(row.data) for row in rows]
    except Exception as e:
        print(f"[DB] db_load_audit_logs error: {e}")
        return []


# ── Citations ────────────────────────────────────────────────────────────────
def db_append_citation(data: dict):
    try:
        with SessionLocal() as session:
            session.add(DBCitation(data=json.dumps(data)))
            session.commit()
    except Exception as e:
        print(f"[DB] db_append_citation error: {e}")


def db_load_citations() -> list:
    try:
        with SessionLocal() as session:
            rows = session.query(DBCitation).all()
            return [json.loads(row.data) for row in rows]
    except Exception as e:
        print(f"[DB] db_load_citations error: {e}")
        return []


# ── Complaints ───────────────────────────────────────────────────────────────
def db_append_complaint(data: dict):
    try:
        with SessionLocal() as session:
            session.add(DBComplaint(data=json.dumps(data)))
            session.commit()
    except Exception as e:
        print(f"[DB] db_append_complaint error: {e}")


def db_load_complaints() -> list:
    try:
        with SessionLocal() as session:
            rows = session.query(DBComplaint).all()
            return [json.loads(row.data) for row in rows]
    except Exception as e:
        print(f"[DB] db_load_complaints error: {e}")
        return []


# ── Login tokens (mobile apps) ──────────────────────────────────────────────
class TokenStore:
    """Dict-like store for Bearer tokens, backed by the database.
    Supports the operations main.py uses: get, [] =, pop, items."""
    MAX_AGE = 30 * 24 * 3600          # tokens expire after 30 days

    def __init__(self, kind: str):
        self.kind = kind

    def get(self, token, default=None):
        if not token:
            return default
        try:
            with SessionLocal() as session:
                row = session.get(DBToken, token)
                if not row or row.kind != self.kind:
                    return default
                if time.time() - row.created > self.MAX_AGE:
                    session.delete(row)
                    session.commit()
                    return default
                return json.loads(row.data)
        except Exception as e:
            print(f"[DB] token get error: {e}")
            return default

    def __setitem__(self, token, value):
        try:
            with SessionLocal() as session:
                row = session.get(DBToken, token)
                if row:
                    row.data, row.kind, row.created = json.dumps(value), self.kind, time.time()
                else:
                    session.add(DBToken(token=token, kind=self.kind, data=json.dumps(value), created=time.time()))
                session.commit()
        except Exception as e:
            print(f"[DB] token save error: {e}")

    def __delitem__(self, token):
        self.pop(token, None)

    def pop(self, token, default=None):
        try:
            with SessionLocal() as session:
                row = session.get(DBToken, token)
                if not row or row.kind != self.kind:
                    return default
                value = json.loads(row.data)
                session.delete(row)
                session.commit()
                return value
        except Exception as e:
            print(f"[DB] token delete error: {e}")
            return default

    def items(self):
        try:
            with SessionLocal() as session:
                rows = session.query(DBToken).filter(DBToken.kind == self.kind).all()
                return [(r.token, json.loads(r.data)) for r in rows]
        except Exception as e:
            print(f"[DB] token list error: {e}")
            return []


# ── Profile pictures ────────────────────────────────────────────────────────
def db_save_avatar(username: str, content_type: str, image: bytes):
    try:
        with SessionLocal() as session:
            row = session.get(DBAvatar, username)
            if row:
                row.content_type, row.image = content_type, image
            else:
                session.add(DBAvatar(username=username, content_type=content_type, image=image))
            session.commit()
    except Exception as e:
        print(f"[DB] db_save_avatar error: {e}")


def db_load_avatar(username: str):
    """Returns (content_type, bytes) or None."""
    try:
        with SessionLocal() as session:
            row = session.get(DBAvatar, username)
            return (row.content_type, row.image) if row else None
    except Exception as e:
        print(f"[DB] db_load_avatar error: {e}")
        return None


def db_delete_avatar(username: str):
    try:
        with SessionLocal() as session:
            row = session.get(DBAvatar, username)
            if row:
                session.delete(row)
                session.commit()
    except Exception as e:
        print(f"[DB] db_delete_avatar error: {e}")
