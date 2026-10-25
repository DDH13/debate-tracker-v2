import argparse
import getpass
import sys

from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.security import hash_password
from app.db.session import engine, init_db
from app.models import User, UserCreate, UserRole


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an admin console user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.BOARD.value)
    args = parser.parse_args()

    password = getpass.getpass("Password (min 8 chars): ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")

    try:
        user_in = UserCreate(email=args.email, name=args.name, role=args.role, password=password)
    except ValidationError as exc:
        sys.exit(str(exc))
    init_db()
    with Session(engine) as session:
        if session.exec(select(User).where(User.email == user_in.email)).first():
            sys.exit(f"A user with email {user_in.email} already exists.")
        user = User.model_validate(user_in, update={"password_hash": hash_password(password)})
        session.add(user)
        session.commit()
        print(f"Created {user.role.value} user {user.email}.")


if __name__ == "__main__":
    main()
