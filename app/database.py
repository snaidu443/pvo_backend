"""SQLAlchemy engine and the per-request database session."""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith('sqlite'):
        engine = create_engine(url, connect_args={'check_same_thread': False})

        @event.listens_for(engine, 'connect')
        def _sqlite_pragmas(dbapi_conn, _):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')  # SQLite ignores foreign keys unless asked
            dbapi_conn.execute('PRAGMA busy_timeout=5000')  # wait up to 5 s for a writer instead of "database is locked"
            if url not in ('sqlite://', 'sqlite:///:memory:'):
                dbapi_conn.execute('PRAGMA journal_mode=WAL')  # readers do not block the upload / extraction writes

        return engine
    # Postgres / Lakebase: small pool, drop dead connections, recycle before token-based logins expire.
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session_factory() -> sessionmaker:
    """Dependency for work that runs after the response (background jobs open their own session)."""
    return SessionLocal


def get_db() -> Iterator[Session]:
    """One session per request. Routes commit explicitly; anything not committed is rolled back."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
