#!/usr/bin/env python3
"""Operator path for issuing an API key without enabling self-registration."""
from __future__ import annotations

import argparse
import uuid

from app import audit
from app.db import SessionLocal
from app.models import User
from app.security import generate_api_key, hash_api_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        plaintext = generate_api_key()
        user = User(
            id=str(uuid.uuid4()),
            name=args.name,
            api_key_hash=hash_api_key(plaintext),
        )
        db.add(user)
        db.flush()
        audit.record(
            db,
            user_id=user.id,
            action="user.register",
            result=audit.OK,
            detail="operator CLI API key issuance",
        )
        db.commit()
        print(f"user_id:  {user.id}")
        print(f"api_key:  {plaintext}   # store now; shown only this once")
    finally:
        db.close()


if __name__ == "__main__":
    main()
