package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"hash/fnv"
	"time"

	"gorm.io/gorm"
)

// ErrLockBusy is returned when another operation currently holds the
// organization lock longer than the configured wait.
var ErrLockBusy = errors.New("organization lock busy (another operation in progress)")

// lockedConn wraps a single pinned *sql.Conn so a GORM session runs every
// statement on the connection that holds the MySQL named lock.
// It satisfies gorm.ConnPool, so gorm.Session{New:true}.Begin() will issue
// BEGIN on this exact connection via the driver interface detection.
type lockedConn struct {
	conn *sql.Conn
	db   *sql.DB
}

func (l *lockedConn) PrepareContext(ctx context.Context, query string) (*sql.Stmt, error) {
	return l.conn.PrepareContext(ctx, query)
}
func (l *lockedConn) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	return l.conn.ExecContext(ctx, query, args...)
}
func (l *lockedConn) QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error) {
	return l.conn.QueryContext(ctx, query, args...)
}
func (l *lockedConn) QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row {
	return l.conn.QueryRowContext(ctx, query, args...)
}
func (l *lockedConn) BeginTx(ctx context.Context, opts *sql.TxOptions) (gorm.ConnPool, error) {
	tx, err := l.conn.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &lockedTx{tx: tx}, nil
}
func (l *lockedConn) GetDBConn() (*sql.DB, error) { return l.db, nil }

type lockedTx struct{ tx *sql.Tx }

func (l *lockedTx) PrepareContext(ctx context.Context, query string) (*sql.Stmt, error) {
	return l.tx.PrepareContext(ctx, query)
}
func (l *lockedTx) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	return l.tx.ExecContext(ctx, query, args...)
}
func (l *lockedTx) QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error) {
	return l.tx.QueryContext(ctx, query, args...)
}
func (l *lockedTx) QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row {
	return l.tx.QueryRowContext(ctx, query, args...)
}
func (l *lockedTx) Commit() error   { return l.tx.Commit() }
func (l *lockedTx) Rollback() error { return l.tx.Rollback() }

// WithOrgLock serializes mutating operations for one organization:
// point batch writes, region publish and reassignment finalization.
//
// fn runs inside a single DB transaction pinned to the connection holding the
// MySQL named lock. The lock is released only after COMMIT, so another waiter
// can never observe an uncommitted catalog flip.
func WithOrgLock(gdb *gorm.DB, orgID uint64, waitSeconds int, fn func(tx *gorm.DB) error) error {
	sqlDB, err := gdb.DB()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(waitSeconds+30)*time.Second)
	defer cancel()

	conn, err := sqlDB.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()

	lockName := orgLockName(orgID)
	var acquired int
	if err := conn.QueryRowContext(ctx,
		fmt.Sprintf("SELECT GET_LOCK('%s', %d)", lockName, waitSeconds)).Scan(&acquired); err != nil {
		return fmt.Errorf("acquire org lock: %w", err)
	}
	if acquired != 1 {
		return ErrLockBusy
	}
	released := false
	release := func() {
		if released {
			return
		}
		var r int
		_ = conn.QueryRowContext(ctx, fmt.Sprintf("SELECT RELEASE_LOCK('%s')", lockName)).Scan(&r)
		released = true
	}
	defer release()

	sess := gdb.Session(&gorm.Session{NewDB: true, Context: ctx})
	sess.ConnPool = &lockedConn{conn: conn, db: sqlDB}
	tx := sess.Begin()
	if tx.Error != nil {
		return tx.Error
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	if err := tx.Commit().Error; err != nil {
		return err
	}
	// Release after commit so waiters only start once the flip is durable.
	release()
	return nil
}

// orgLockName derives a legal MySQL lock identifier. GET_LOCK names are
// arbitrary strings, but we keep them short and stable per org.
func orgLockName(orgID uint64) string {
	h := fnv.New64a()
	fmt.Fprintf(h, "geoterritory-org-%d", orgID)
	return fmt.Sprintf("gtorg%d", h.Sum64()%1_000_000_000)
}
