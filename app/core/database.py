from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session
from typing import Generator


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory = None


def get_engine(database_url: str | None = None):
    global _engine
    if _engine is None:
        from app.core.settings import get_settings
        url = database_url or get_settings().database_url
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, echo=False, **kwargs)
        # Enable WAL mode for SQLite for better concurrency
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def set_wal(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return _engine


def get_session_factory(database_url: str | None = None) -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(database_url),
        )
    return _SessionFactory


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_db(database_url: str | None = None) -> None:
    from app.models import canonical  # noqa: registers all ORM models with Base
    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)


def reset_engine() -> None:
    """For testing: tear down the cached engine so a new one can be created."""
    global _engine, _SessionFactory
    if _engine:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
