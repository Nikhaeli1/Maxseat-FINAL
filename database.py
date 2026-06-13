"""
database.py — Persistent storage for MaxSeat Alert System
Uses PostgreSQL on Railway (via DATABASE_URL env var).
Falls back to SQLite for local development when DATABASE_URL is not set.
"""

import os, json
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

# ── Connection setup ────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Railway sets postgres:// — SQLAlchemy 1.4+ needs postgresql+psycopg2://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# Local dev fallback: SQLite file in the project root
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./maxseat.db"

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


def db_load_audit_logs() -> list:
    try:
        with SessionLocal() as session:
            rows = session.query(DBAuditLog).order_by(DBAuditLog.id.desc()).all()
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
