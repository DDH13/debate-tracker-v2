import argparse

from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.db.seed import seed as seed_data
from app.db.session import engine


def create() -> None:
    SQLModel.metadata.create_all(engine)
    print("Tables created.")


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
