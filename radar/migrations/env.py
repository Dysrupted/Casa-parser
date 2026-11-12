from alembic import context
from sqlmodel import SQLModel

from radar import models  # noqa: F401  (register tables)

config = context.config
target_metadata = SQLModel.metadata


def run_migrations() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        from radar.db import make_engine
        connectable = make_engine(config.get_main_option("sqlalchemy.url"))
        with connectable.connect() as conn:
            _run(conn)
    else:
        _run(connectable)


def _run(conn) -> None:
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
