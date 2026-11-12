from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from radar import models  # noqa: F401  (register tables)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        if ":memory:" not in url:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
