import getpass
import os
import re

from argon2 import PasswordHasher
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .models import User


def main():
    if os.getenv("DATABASE_URL") is None:
        raise SystemExit("DATABASE_URL is required")
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with Session(engine) as db:
        if db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)):
            raise SystemExit("An administrator already exists; refusing to create another via bootstrap command.")
        username = input("First administrator username (3-32 letters, numbers, underscore): ").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise SystemExit("Invalid username.")
        if db.scalar(select(User.id).where(User.username == username)):
            raise SystemExit("That username is already registered.")
        password = getpass.getpass("New administrator password (at least 12 characters): ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm or len(password) < 12 or len(password) > 128:
            raise SystemExit("Passwords must match and contain 12-128 characters.")
        user = User(username=username, password_hash=PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password), is_admin=True)
        db.add(user)
        db.commit()
        print(f"Administrator {username!r} created. Store the password in your password manager.")


if __name__ == "__main__":
    main()
