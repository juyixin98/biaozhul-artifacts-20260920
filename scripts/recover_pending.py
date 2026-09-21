#!/usr/bin/env python3
"""Administrative recovery for sign requests left `pending` by a crash.

Usage:
    python scripts/recover_pending.py list
    python scripts/recover_pending.py resume <sign_request_id>
    python scripts/recover_pending.py release <sign_request_id>

* list     - show pending requests older than --older-seconds (default 60s)
* resume   - re-run signing on the SAME occupied row (no double debit)
* release  - abandon the hold; quota and nonce are recovered

Runs inside the API process's database; no chain access whatsoever.
"""
from __future__ import annotations

import argparse
import datetime as dt

from sqlalchemy import select

from app.db import SessionLocal
from app.models import REQ_PENDING, SignRequest
from app.services import release_pending, resume_sign


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "resume", "release"])
    parser.add_argument("request_id", nargs="?", default=None)
    parser.add_argument("--older-seconds", type=int, default=60)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.command == "list":
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                seconds=args.older_seconds
            )
            rows = db.scalars(
                select(SignRequest)
                .where(SignRequest.status == REQ_PENDING)
                .order_by(SignRequest.created_at)
            ).all()
            stuck = [r for r in rows if r.created_at <= cutoff]
            print(f"{len(stuck)} pending request(s) older than {args.older_seconds}s")
            for r in stuck:
                print(
                    f"  {r.id}  wallet={r.wallet_id} chain={int(r.chain_id)} "
                    f"nonce={int(r.nonce)} value={int(r.value_wei)} "
                    f"created={r.created_at.isoformat()}"
                )
            return

        if not args.request_id:
            parser.error(f"{args.command} requires a sign request id")

        if args.command == "resume":
            target = db.get(SignRequest, args.request_id)
            if target is None:
                raise SystemExit("sign request not found")
            req = resume_sign(
                db,
                user_id=target.user_id,
                sign_request_id=args.request_id,
                request_id=None,
            )
            print(f"resumed -> {req.status} tx_hash={req.tx_hash}")
        else:
            req = db.get(SignRequest, args.request_id)
            if req is None:
                raise SystemExit("not found")
            req = release_pending(
                db,
                user_id=req.user_id,
                sign_request_id=args.request_id,
                request_id=None,
            )
            print(f"released -> {req.status}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
