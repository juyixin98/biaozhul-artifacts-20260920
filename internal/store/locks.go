package store

import (
	"context"

	"github.com/jmoiron/sqlx"
)

// SessionLocks runs guarded work under a Postgres session-level advisory lock
// pinned to one dedicated connection. pg_advisory_lock is session-scoped, so
// locking and unlocking must happen on the SAME backend connection; taking a
// dedicated conn from the pool for the guard's lifetime guarantees that.
type SessionLocks struct {
	DB *sqlx.DB
}

func NewSessionLocks(db *sqlx.DB) *SessionLocks { return &SessionLocks{DB: db} }

// WithSessionLock attempts to acquire the lock; when acquired it runs fn and
// then releases the lock. ran is false when another session already holds it
// (fn is not run).
func (l *SessionLocks) WithSessionLock(ctx context.Context, id int64, fn func() error) (ran bool, err error) {
	conn, err := l.DB.Conn(ctx)
	if err != nil {
		return false, err
	}
	defer conn.Close()

	var got bool
	if err = conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, id).Scan(&got); err != nil {
		return false, err
	}
	if !got {
		return false, nil
	}
	defer func() {
		var unlocked bool
		if uErr := conn.QueryRowContext(ctx, `SELECT pg_advisory_unlock($1)`, id).Scan(&unlocked); uErr != nil && err == nil {
			err = uErr
		}
	}()

	if err = fn(); err != nil {
		return true, err
	}
	return true, nil
}
