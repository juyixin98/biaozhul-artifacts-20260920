"""Retry helpers for database lock contention.

Under MySQL/InnoDB two transactions can hit:

* 1213 deadlock -- InnoDB chose a victim and rolled it back;
* 1205 lock wait timeout -- a ``SELECT ... FOR UPDATE`` waited too long.

Both leave the transaction rolled back with no partial writes, so the whole
atomic operation can safely be retried.  This is standard behaviour for
applications serialising on row locks: under contention a bounded number of
retries makes submit/cancel effectively deterministic to the caller.
"""
import time

from django.db import OperationalError, InternalError, DatabaseError

LOCK_ERROR_CODES = {1205, 1213}


def is_lock_error(exc) -> bool:
    code = getattr(exc, "args", [None])[0]
    if code in LOCK_ERROR_CODES:
        return True
    # PyMySQL sometimes wraps the error code differently.
    text = str(exc)
    return ("Deadlock found" in text) or ("Lock wait timeout" in text)


def retry_on_lock(fn, *, attempts=8, base_delay=0.02):
    """Run callable ``fn`` retrying on InnoDB deadlock / lock-wait timeout."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except (OperationalError, InternalError, DatabaseError) as exc:
            last_exc = exc
            if not is_lock_error(exc):
                raise
            from django.db import connection

            # Make sure the poisoned connection is reset before retry.
            connection.rollback()
            time.sleep(base_delay * (2 ** i))
    raise last_exc
