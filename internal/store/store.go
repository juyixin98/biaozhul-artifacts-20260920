// Package store persists verified OCI graphs, movable tags and digest-bound
// resolution tasks in SQLite.
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"ociarch/internal/oci"
)

const schema = `
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes (
  digest     TEXT PRIMARY KEY,
  media_type TEXT NOT NULL,
  size       INTEGER NOT NULL,
  json       TEXT          -- raw document for index/manifest/config; NULL for layers
);
CREATE TABLE IF NOT EXISTS edges (
  parent   TEXT NOT NULL,
  child    TEXT NOT NULL,
  kind     TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  os       TEXT NOT NULL DEFAULT '',
  arch     TEXT NOT NULL DEFAULT '',
  variant  TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (parent, child, kind, position)
);
CREATE TABLE IF NOT EXISTS tags (
  name       TEXT PRIMARY KEY,
  digest     TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  digest     TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolutions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  requested_ref   TEXT NOT NULL,   -- tag or digest as requested
  root_digest     TEXT NOT NULL,   -- digest bound at resolve time
  os              TEXT NOT NULL,
  arch            TEXT NOT NULL,
  variant         TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL,   -- resolved | ambiguous | no_match
  selected_digest TEXT NOT NULL DEFAULT '',
  error           TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolution_chain (
  resolution_id INTEGER NOT NULL,
  seq           INTEGER NOT NULL,
  digest        TEXT NOT NULL,
  media_type    TEXT NOT NULL,
  kind          TEXT NOT NULL,
  size          INTEGER NOT NULL,
  PRIMARY KEY (resolution_id, seq)
);
`

type Store struct {
	db *sql.DB
}

func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("init schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func now() string { return time.Now().UTC().Format(time.RFC3339Nano) }

// SaveGraph atomically stores a verified graph and points tag at its root.
// Tags are movable: re-importing under the same name updates the pointer and
// appends to tag_history; previously created resolutions keep their digest.
func (s *Store) SaveGraph(g *oci.Graph, tag string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	for _, n := range g.Nodes {
		var rawJSON any
		if n.Raw != nil {
			rawJSON = string(n.Raw)
		}
		if _, err := tx.Exec(
			`INSERT INTO nodes(digest, media_type, size, json) VALUES(?,?,?,?)
			 ON CONFLICT(digest) DO NOTHING`,
			n.Digest, n.MediaType, n.Size, rawJSON); err != nil {
			return fmt.Errorf("insert node %s: %w", n.Digest, err)
		}
	}
	for _, e := range g.Edges {
		var os_, arch, variant string
		if e.Platform != nil {
			os_, arch, variant = e.Platform.OS, e.Platform.Architecture, e.Platform.Variant
		}
		if _, err := tx.Exec(
			`INSERT INTO edges(parent, child, kind, position, os, arch, variant)
			 VALUES(?,?,?,?,?,?,?)
			 ON CONFLICT(parent, child, kind, position) DO NOTHING`,
			e.Parent, e.Child, e.Kind, e.Position, os_, arch, variant); err != nil {
			return fmt.Errorf("insert edge %s->%s: %w", e.Parent, e.Child, err)
		}
	}
	if tag != "" {
		t := now()
		if _, err := tx.Exec(
			`INSERT INTO tags(name, digest, updated_at) VALUES(?,?,?)
			 ON CONFLICT(name) DO UPDATE SET digest=excluded.digest, updated_at=excluded.updated_at`,
			tag, g.Root, t); err != nil {
			return fmt.Errorf("upsert tag %s: %w", tag, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO tag_history(name, digest, changed_at) VALUES(?,?,?)`,
			tag, g.Root, t); err != nil {
			return fmt.Errorf("insert tag history %s: %w", tag, err)
		}
	}
	return tx.Commit()
}

// IndexManifests returns the manifest descriptors of the index at digest.
func (s *Store) IndexManifests(indexDigest string) ([]oci.Descriptor, error) {
	rows, err := s.db.Query(
		`SELECT child, os, arch, variant FROM edges
		 WHERE parent=? AND kind=? ORDER BY position`, indexDigest, oci.EdgeManifest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []oci.Descriptor
	for rows.Next() {
		var d oci.Descriptor
		var p oci.Platform
		if err := rows.Scan(&d.Digest, &p.OS, &p.Architecture, &p.Variant); err != nil {
			return nil, err
		}
		d.Platform = &p
		out = append(out, d)
	}
	return out, rows.Err()
}

// Chain reconstructs the ordered dependency chain root -> manifest -> config
// -> layers from stored edges.
func (s *Store) Chain(rootDigest, manifestDigest string) ([]ChainEntry, error) {
	var out []ChainEntry
	push := func(digest, kind string, seq *int) error {
		var mt string
		var size int64
		err := s.db.QueryRow(`SELECT media_type, size FROM nodes WHERE digest=?`, digest).Scan(&mt, &size)
		if err != nil {
			return fmt.Errorf("load node %s: %w", digest, err)
		}
		out = append(out, ChainEntry{Seq: *seq, Digest: digest, MediaType: mt, Kind: kind, Size: size})
		*seq++
		return nil
	}
	seq := 0
	if err := push(rootDigest, "root", &seq); err != nil {
		return nil, err
	}
	if err := push(manifestDigest, oci.EdgeManifest, &seq); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT child, kind FROM edges WHERE parent=? AND kind IN (?, ?)
		 ORDER BY CASE kind WHEN ? THEN 0 ELSE 1 END, position`,
		manifestDigest, oci.EdgeConfig, oci.EdgeLayer, oci.EdgeConfig)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var child, kind string
		if err := rows.Scan(&child, &kind); err != nil {
			return nil, err
		}
		if err := push(child, kind, &seq); err != nil {
			return nil, err
		}
	}
	return out, rows.Err()
}

// TagDigest resolves a tag to its current digest.
func (s *Store) TagDigest(name string) (string, error) {
	var d string
	err := s.db.QueryRow(`SELECT digest FROM tags WHERE name=?`, name).Scan(&d)
	if err == sql.ErrNoRows {
		return "", fmt.Errorf("unknown tag %q", name)
	}
	return d, err
}

type TagInfo struct {
	Name      string `json:"name"`
	Digest    string `json:"digest"`
	UpdatedAt string `json:"updated_at"`
}

func (s *Store) ListTags() ([]TagInfo, error) {
	rows, err := s.db.Query(`SELECT name, digest, updated_at FROM tags ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TagInfo
	for rows.Next() {
		var t TagInfo
		if err := rows.Scan(&t.Name, &t.Digest, &t.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

type TagHistoryEntry struct {
	Name      string `json:"name"`
	Digest    string `json:"digest"`
	ChangedAt string `json:"changed_at"`
}

func (s *Store) TagHistory(name string) ([]TagHistoryEntry, error) {
	rows, err := s.db.Query(
		`SELECT name, digest, changed_at FROM tag_history WHERE name=? ORDER BY id`, name)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TagHistoryEntry
	for rows.Next() {
		var h TagHistoryEntry
		if err := rows.Scan(&h.Name, &h.Digest, &h.ChangedAt); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// HasNode reports whether a digest is known (e.g. resolving by digest).
func (s *Store) HasNode(digest string) (bool, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(1) FROM nodes WHERE digest=?`, digest).Scan(&n)
	return n > 0, err
}

// NodeMediaType returns the media type of a stored node.
func (s *Store) NodeMediaType(digest string) (string, error) {
	var mt string
	err := s.db.QueryRow(`SELECT media_type FROM nodes WHERE digest=?`, digest).Scan(&mt)
	return mt, err
}

type ChainEntry struct {
	Seq       int    `json:"seq"`
	Digest    string `json:"digest"`
	MediaType string `json:"media_type"`
	Kind      string `json:"kind"`
	Size      int64  `json:"size"`
}

type Resolution struct {
	ID             int64        `json:"id"`
	RequestedRef   string       `json:"requested_ref"`
	RootDigest     string       `json:"root_digest"`
	OS             string       `json:"os"`
	Architecture   string       `json:"architecture"`
	Variant        string       `json:"variant,omitempty"`
	Status         string       `json:"status"`
	SelectedDigest string       `json:"selected_digest,omitempty"`
	Error          string       `json:"error,omitempty"`
	CreatedAt      string       `json:"created_at"`
	Chain          []ChainEntry `json:"chain,omitempty"`
}

// CreateResolution inserts a resolution task and (for resolved ones) its
// frozen dependency chain. The chain is copied at resolve time so later tag
// moves or re-imports can never mutate what this task saw.
func (s *Store) CreateResolution(r *Resolution) (int64, error) {
	tx, err := s.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	ts := now()
	res, err := tx.Exec(
		`INSERT INTO resolutions(requested_ref, root_digest, os, arch, variant, status, selected_digest, error, created_at)
		 VALUES(?,?,?,?,?,?,?,?,?)`,
		r.RequestedRef, r.RootDigest, r.OS, r.Architecture, r.Variant,
		r.Status, r.SelectedDigest, r.Error, ts)
	if err != nil {
		return 0, err
	}
	r.CreatedAt = ts
	id, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	for _, c := range r.Chain {
		if _, err := tx.Exec(
			`INSERT INTO resolution_chain(resolution_id, seq, digest, media_type, kind, size)
			 VALUES(?,?,?,?,?,?)`,
			id, c.Seq, c.Digest, c.MediaType, c.Kind, c.Size); err != nil {
			return 0, err
		}
	}
	return id, tx.Commit()
}

func (s *Store) GetResolution(id int64) (*Resolution, error) {
	var r Resolution
	err := s.db.QueryRow(
		`SELECT id, requested_ref, root_digest, os, arch, variant, status, selected_digest, error, created_at
		 FROM resolutions WHERE id=?`, id).
		Scan(&r.ID, &r.RequestedRef, &r.RootDigest, &r.OS, &r.Architecture,
			&r.Variant, &r.Status, &r.SelectedDigest, &r.Error, &r.CreatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT seq, digest, media_type, kind, size FROM resolution_chain
		 WHERE resolution_id=? ORDER BY seq`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var c ChainEntry
		if err := rows.Scan(&c.Seq, &c.Digest, &c.MediaType, &c.Kind, &c.Size); err != nil {
			return nil, err
		}
		r.Chain = append(r.Chain, c)
	}
	return &r, rows.Err()
}

func (s *Store) ListResolutions() ([]Resolution, error) {
	rows, err := s.db.Query(
		`SELECT id, requested_ref, root_digest, os, arch, variant, status, selected_digest, error, created_at
		 FROM resolutions ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Resolution
	for rows.Next() {
		var r Resolution
		if err := rows.Scan(&r.ID, &r.RequestedRef, &r.RootDigest, &r.OS, &r.Architecture,
			&r.Variant, &r.Status, &r.SelectedDigest, &r.Error, &r.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// DebugJSON is a helper for tests.
func (r *Resolution) String() string {
	b, _ := json.Marshal(r)
	return string(b)
}
