import argparse

from sqlalchemy import inspect
from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.db.seed import seed as seed_data
from app.db.session import engine
from app.services.finance import ensure_default_categories


def create() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        added = ensure_default_categories(session)
    print(f"Tables created. {added} default finance categories added.")


def indexes() -> None:
    """Adds any model index missing from the existing tables (`create_all` never does).
    Create-if-not-exists, so it is safe to re-run; it never drops or alters anything."""
    existing = set(inspect(engine).get_table_names())
    created = 0
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {ix["name"] for ix in inspect(engine).get_indexes(table.name)}
        for index in table.indexes:
            if index.name in have:
                continue
            index.create(bind=engine, checkfirst=True)
            created += 1
    print(f"{created} index(es) created.")


def drop() -> None:
    SQLModel.metadata.drop_all(engine)
    print("Tables dropped.")


def truncate() -> None:
    table_names = [t.name for t in SQLModel.metadata.sorted_tables]
    if not table_names:
        print("No tables to truncate.")
        return
    with engine.begin() as conn:
        if settings.database_url.startswith("sqlite"):
            for name in table_names:
                conn.exec_driver_sql(f'DELETE FROM "{name}"')
        else:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            conn.exec_driver_sql(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
    print("Tables truncated.")


def seed() -> None:
    with Session(engine) as session:
        seed_data(session)
    print("Sample data seeded.")


def reset() -> None:
    drop()
    create()
    seed()


COMMANDS = {
    "create": create,
    "indexes": indexes,
    "drop": drop,
    "truncate": truncate,
    "seed": seed,
    "reset": reset,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Database maintenance commands.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    COMMANDS[args.command]()


if __name__ == "__main__":
    main()
