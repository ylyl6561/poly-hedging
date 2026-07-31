from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema() -> None:
    Base.metadata.create_all(get_engine())


def reset_database_caches() -> None:
    get_session_factory.cache_clear()
    engine = get_engine.cache_info()
    if engine.currsize:
        get_engine().dispose()
    get_engine.cache_clear()
