// Package store persists mutable tag pointers and resolved platform-selection
// tasks in SQLite. Blobs themselves live in the OCI image-layout directories
// managed by the blobstore package; this database only holds the mutable and
// historical metadata.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

// timestamp returns a UTC RFC3339 timestamp with millisecond precision.
func timestamp() string { return time.Now().UTC().Format("2006-01-02T15:04:05.000Z") }

// Store is the SQLite-backed metadata store.
type Store struct {
	db *sql.DB
}

// ErrNotFound is returned when a requested row does not exist.
var ErrNotFound = errors.New("not found")

// Open opens (creating the schema if needed) the database at dsn.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening sqlite %q: %w", dsn, err)
	}
	// A single connection serializes writes and makes transaction semantics
	// predictable for the embedded database.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, `PRAGMA journal_mode=WAL;`); err != nil {
		db.Close()
		return nil, err
	}
	if _, err := db.ExecContext(ctx, `PRAGMA foreign_keys=ON;`); err != nil {
		db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS tags (
			repository TEXT NOT NULL,
			tag        TEXT NOT NULL,
			digest     TEXT NOT NULL,
			media_type TEXT NOT NULL DEFAULT '',
			updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
			PRIMARY KEY (repository, tag)
		);`,
		`CREATE TABLE IF NOT EXISTS tasks (
			id          TEXT PRIMARY KEY,
			repository  TEXT NOT NULL,
			reference   TEXT NOT NULL,
			resolved_tag TEXT NOT NULL DEFAULT '',
			root_digest TEXT NOT NULL,
			os          TEXT NOT NULL,
			architecture TEXT NOT NULL,
			variant     TEXT NOT NULL DEFAULT '',
			status      TEXT NOT NULL,
			error_code  TEXT NOT NULL DEFAULT '',
			error_detail TEXT NOT NULL DEFAULT '',
			manifest_digest TEXT NOT NULL DEFAULT '',
			created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		);`,
		`CREATE TABLE IF NOT EXISTS task_chain (
			task_id  TEXT NOT NULL,
			position INTEGER NOT NULL,
			role     TEXT NOT NULL,
			digest   TEXT NOT NULL,
			size     INTEGER NOT NULL,
			media_type TEXT NOT NULL DEFAULT '',
			PRIMARY KEY (task_id, position),
			FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_tasks_repo ON tasks(repository, created_at);`,
	}
	for _, stmt := range stmts {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("migration failed: %w", err)
		}
	}
	return nil
}

// Tag is a mutable tag -> manifest/index digest pointer.
type Tag struct {
	Repository string
	Tag        string
	Digest     string
	MediaType  string
	UpdatedAt  string
}

// UpsertTag points tag at digest within repository.
func (s *Store) UpsertTag(ctx context.Context, t Tag) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO tags(repository, tag, digest, media_type, updated_at)
		VALUES(?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		ON CONFLICT(repository, tag) DO UPDATE SET
			digest=excluded.digest,
			media_type=excluded.media_type,
			updated_at=excluded.updated_at`,
		t.Repository, t.Tag, t.Digest, t.MediaType)
	return err
}

// GetTag resolves a tag to its current digest.
func (s *Store) GetTag(ctx context.Context, repository, tag string) (Tag, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT repository, tag, digest, media_type, updated_at
		FROM tags WHERE repository = ? AND tag = ?`, repository, tag)
	var t Tag
	if err := row.Scan(&t.Repository, &t.Tag, &t.Digest, &t.MediaType, &t.UpdatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Tag{}, fmt.Errorf("tag %s/%s: %w", repository, tag, ErrNotFound)
		}
		return Tag{}, err
	}
	return t, nil
}

// ListTags returns tags of a repository ordered by name.
func (s *Store) ListTags(ctx context.Context, repository string) ([]Tag, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT repository, tag, digest, media_type, updated_at
		FROM tags WHERE repository = ? ORDER BY tag`, repository)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Tag
	for rows.Next() {
		var t Tag
		if err := rows.Scan(&t.Repository, &t.Tag, &t.Digest, &t.MediaType, &t.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// ChainEntry mirrors resolver.ChainEntry for persistence.
type ChainEntry struct {
	Position  int
	Role      string
	Digest    string
	Size      int64
	MediaType string
}

// Task is a recorded platform-resolution task.
type Task struct {
	ID             string
	Repository     string
	Reference      string
	ResolvedTag    string
	RootDigest     string
	OS             string
	Architecture   string
	Variant        string
	Status         string
	ErrorCode      string
	ErrorDetail    string
	ManifestDigest string
	CreatedAt      string
	Chain          []ChainEntry
}

// CreateTask inserts a task and its dependency chain atomically.
func (s *Store) CreateTask(ctx context.Context, t *Task) error {
	if t.CreatedAt == "" {
		t.CreatedAt = timestamp()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO tasks(id, repository, reference, resolved_tag, root_digest, os, architecture,
		                  variant, status, error_code, error_detail, manifest_digest, created_at)
		VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.Repository, t.Reference, t.ResolvedTag, t.RootDigest, t.OS, t.Architecture,
		t.Variant, t.Status, t.ErrorCode, t.ErrorDetail, t.ManifestDigest, t.CreatedAt); err != nil {
		return err
	}
	for _, c := range t.Chain {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO task_chain(task_id, position, role, digest, size, media_type)
			VALUES(?, ?, ?, ?, ?, ?)`,
			t.ID, c.Position, c.Role, c.Digest, c.Size, c.MediaType); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// GetTask returns a task and its dependency chain.
func (s *Store) GetTask(ctx context.Context, id string) (*Task, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT id, repository, reference, resolved_tag, root_digest, os, architecture, variant,
		       status, error_code, error_detail, manifest_digest, created_at
		FROM tasks WHERE id = ?`, id)
	var t Task
	if err := row.Scan(&t.ID, &t.Repository, &t.Reference, &t.ResolvedTag, &t.RootDigest,
		&t.OS, &t.Architecture, &t.Variant, &t.Status, &t.ErrorCode, &t.ErrorDetail,
		&t.ManifestDigest, &t.CreatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, fmt.Errorf("task %s: %w", id, ErrNotFound)
		}
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT position, role, digest, size, media_type
		FROM task_chain WHERE task_id = ? ORDER BY position`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var c ChainEntry
		if err := rows.Scan(&c.Position, &c.Role, &c.Digest, &c.Size, &c.MediaType); err != nil {
			return nil, err
		}
		t.Chain = append(t.Chain, c)
	}
	return &t, rows.Err()
}

// ListTasks returns the most recent tasks of a repository, newest first.
func (s *Store) ListTasks(ctx context.Context, repository string, limit int) ([]*Task, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, repository, reference, resolved_tag, root_digest, os, architecture, variant,
		       status, error_code, error_detail, manifest_digest, created_at
		FROM tasks WHERE repository = ? ORDER BY created_at DESC, id DESC LIMIT ?`,
		repository, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Task
	for rows.Next() {
		var t Task
		if err := rows.Scan(&t.ID, &t.Repository, &t.Reference, &t.ResolvedTag, &t.RootDigest,
			&t.OS, &t.Architecture, &t.Variant, &t.Status, &t.ErrorCode, &t.ErrorDetail,
			&t.ManifestDigest, &t.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, &t)
	}
	return out, rows.Err()
}
