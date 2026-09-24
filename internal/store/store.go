// Package store is the PostgreSQL persistence layer: schema migration,
// content/tag/upload/lease operations, advisory locks that serialize GC
// against publishers and pulls, and the recursive reachability query the GC
// marker/sweeper use.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"hash/fnv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schemaSQL string

// Store wraps the connection pool.
type Store struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, url string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 20
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	s := &Store{pool: pool}
	if err := s.ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	if err := s.migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close()              { s.pool.Close() }
func (s *Store) Pool() *pgxpool.Pool { return s.pool }

func (s *Store) ping(ctx context.Context) error {
	return s.pool.AcquireFunc(ctx, func(c *pgxpool.Conn) error {
		return c.Ping(ctx)
	})
}

func (s *Store) migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, schemaSQL)
	return err
}

// ---------------------------------------------------------------------------
// Advisory locks.
//
// Advisory locks are taken in the single-bigint form so that a hash can be
// used. Key layout: top 2 bits select the namespace, low 61 bits the payload,
// so every value fits in a signed bigint.
const (
	nsGC       = 0
	nsPublish  = 1
	nsBlob     = 2
	nsManifest = 3
)

func lockKey(s string) int64 {
	h := fnv.New64a()
	h.Write([]byte(s))
	return int64(h.Sum64() & 0x1fffffffffffffff) // 61-bit payload
}

func publishLockKey() int64          { return int64(nsPublish) << 61 }
func gcRunLockKey() int64            { return int64(nsGC) << 61 }
func blobLockKey(d string) int64     { return (int64(nsBlob) << 61) | lockKey(d) }
func manifestLockKey(k string) int64 { return (int64(nsManifest) << 61) | lockKey(k) }

// TxPublishLock is a transaction-scoped global lock taken by every publishing
// path (blob finalize, manifest PUT) and by the GC sweeper per item. It
// establishes a total order between "the mark snapshot" and "publish commit":
// a publisher committing while a run is sweeping is visible at sweep time.
func TxPublishLock(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", publishLockKey())
	return err
}

// Blob locks: session-scoped so a pull can hold the lock across streaming
// while its read lease exists.
func (s *Store) BlobLock(ctx context.Context, conn *pgxpool.Conn, digest string) error {
	_, err := conn.Exec(ctx, "SELECT pg_advisory_lock($1)", blobLockKey(digest))
	return err
}

func (s *Store) BlobUnlock(ctx context.Context, conn *pgxpool.Conn, digest string) error {
	_, err := conn.Exec(ctx, "SELECT pg_advisory_unlock($1)", blobLockKey(digest))
	return err
}

// TxBlobLock is a transaction-scoped blob lock (sweep delete / API delete).
func TxBlobLock(ctx context.Context, tx pgx.Tx, digest string) error {
	_, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", blobLockKey(digest))
	return err
}

func manifestKey(repo, digest string) string { return repo + "\x00" + digest }

// Manifest locks follow the same session/tx split as blob locks.
func (s *Store) ManifestLock(ctx context.Context, conn *pgxpool.Conn, repo, digest string) error {
	_, err := conn.Exec(ctx, "SELECT pg_advisory_lock($1)", manifestLockKey(manifestKey(repo, digest)))
	return err
}

func (s *Store) ManifestUnlock(ctx context.Context, conn *pgxpool.Conn, repo, digest string) error {
	_, err := conn.Exec(ctx, "SELECT pg_advisory_unlock($1)", manifestLockKey(manifestKey(repo, digest)))
	return err
}

func TxManifestLock(ctx context.Context, tx pgx.Tx, repo, digest string) error {
	_, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", manifestLockKey(manifestKey(repo, digest)))
	return err
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

var (
	ErrNotFound      = errors.New("not found")
	ErrAlreadyExists = errors.New("already exists")
)

// ---------------------------------------------------------------------------
// Blobs
// ---------------------------------------------------------------------------

func (s *Store) PublishBlob(ctx context.Context, digest string, size int64, createdAt time.Time) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO blobs(digest, size, created_at)
		 VALUES ($1,$2,$3) ON CONFLICT (digest) DO NOTHING`,
		digest, size, createdAt)
	return err
}

func (s *Store) GetBlobSize(ctx context.Context, digest string) (int64, error) {
	var size int64
	err := s.pool.QueryRow(ctx, "SELECT size FROM blobs WHERE digest=$1", digest).Scan(&size)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, ErrNotFound
	}
	return size, err
}

// BlobInfo is one catalog row.
type BlobInfo struct {
	Digest    string    `json:"digest"`
	Size      int64     `json:"size"`
	CreatedAt time.Time `json:"created_at"`
}

func (s *Store) ListBlobs(ctx context.Context) ([]BlobInfo, error) {
	rows, err := s.pool.Query(ctx, "SELECT digest, size, created_at FROM blobs ORDER BY digest")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []BlobInfo
	for rows.Next() {
		var b BlobInfo
		if err := rows.Scan(&b.Digest, &b.Size, &b.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

func (s *Store) DeleteBlobRow(ctx context.Context, tx pgx.Tx, digest string) error {
	_, err := tx.Exec(ctx, "DELETE FROM blobs WHERE digest=$1", digest)
	return err
}

// ---------------------------------------------------------------------------
// Manifests & tags
// ---------------------------------------------------------------------------

// UpsertManifest inserts a manifest and its reference edges, then points the
// tag (when reference is a tag name) at it. Must run inside the caller's
// transaction which already holds the global publish lock.
func (s *Store) UpsertManifest(ctx context.Context, tx pgx.Tx,
	repo, digest, mediaType string, size int64, content []byte,
	refs []ChildRef, tagName string,
) error {
	if _, err := tx.Exec(ctx,
		`INSERT INTO manifests(repo, digest, media_type, size, content)
		 VALUES ($1,$2,$3,$4,$5)
		 ON CONFLICT (repo, digest) DO UPDATE SET media_type=EXCLUDED.media_type, size=EXCLUDED.size, content=EXCLUDED.content`,
		repo, digest, mediaType, size, content); err != nil {
		return err
	}
	for _, r := range refs {
		if _, err := tx.Exec(ctx,
			`INSERT INTO manifest_refs(repo, parent_digest, child_kind, child_digest)
			 VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING`,
			repo, digest, r.Kind, r.Digest); err != nil {
			return err
		}
	}
	if tagName != "" {
		if _, err := tx.Exec(ctx,
			`INSERT INTO tags(repo, name, digest) VALUES ($1,$2,$3)
			 ON CONFLICT (repo, name) DO UPDATE SET digest=EXCLUDED.digest, updated_at=now()`,
			repo, tagName, digest); err != nil {
			return err
		}
	}
	return nil
}

// ChildRef mirrors manifestx.ChildRef (kept as a store-local DTO so the store
// package does not depend on the parser package).
type ChildRef struct {
	Kind   string
	Digest string
}

// ChildExists checks every referenced object exists before a manifest is
// accepted ("referential integrity" on publish, mirroring distribution's
// manifest verification).
func (s *Store) ChildExists(ctx context.Context, tx pgx.Tx, repo string, r ChildRef) (bool, error) {
	switch r.Kind {
	case "blob":
		var ok bool
		err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", r.Digest).Scan(&ok)
		return ok, err
	case "manifest":
		var ok bool
		err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM manifests WHERE repo=$1 AND digest=$2)", repo, r.Digest).Scan(&ok)
		return ok, err
	default:
		return false, fmt.Errorf("unknown child kind %q", r.Kind)
	}
}

type ManifestRow struct {
	Repo      string
	Digest    string
	MediaType string
	Size      int64
	Content   []byte
	CreatedAt time.Time
}

func (s *Store) GetManifest(ctx context.Context, repo, digest string) (*ManifestRow, error) {
	var m ManifestRow
	err := s.pool.QueryRow(ctx,
		"SELECT repo, digest, media_type, size, content, created_at FROM manifests WHERE repo=$1 AND digest=$2",
		repo, digest).Scan(&m.Repo, &m.Digest, &m.MediaType, &m.Size, &m.Content, &m.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &m, nil
}

// ResolveTag maps a tag name to its manifest digest.
func (s *Store) ResolveTag(ctx context.Context, repo, name string) (string, error) {
	var digest string
	err := s.pool.QueryRow(ctx, "SELECT digest FROM tags WHERE repo=$1 AND name=$2", repo, name).Scan(&digest)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", ErrNotFound
	}
	return digest, err
}

// ResolveReference accepts tag or digest.
func (s *Store) ResolveReference(ctx context.Context, repo, ref string) (string, error) {
	if len(ref) > 7 && ref[:7] == "sha256:" {
		ok, err := s.ManifestExists(ctx, repo, ref)
		if err != nil {
			return "", err
		}
		if !ok {
			return "", ErrNotFound
		}
		return ref, nil
	}
	return s.ResolveTag(ctx, repo, ref)
}

func (s *Store) ManifestExists(ctx context.Context, repo, digest string) (bool, error) {
	var ok bool
	err := s.pool.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM manifests WHERE repo=$1 AND digest=$2)", repo, digest).Scan(&ok)
	return ok, err
}

func (s *Store) DeleteManifestCascade(ctx context.Context, tx pgx.Tx, repo, digest string) error {
	_, err := tx.Exec(ctx, "DELETE FROM manifests WHERE repo=$1 AND digest=$2", repo, digest)
	return err
}

func (s *Store) DeleteTagOrManifest(ctx context.Context, repo, ref string) (string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	if err := TxPublishLock(ctx, tx); err != nil {
		return "", err
	}
	var digest string
	if len(ref) > 7 && ref[:7] == "sha256:" {
		digest = ref
	} else {
		err := tx.QueryRow(ctx, "SELECT digest FROM tags WHERE repo=$1 AND name=$2", repo, ref).Scan(&digest)
		if errors.Is(err, pgx.ErrNoRows) {
			return "", ErrNotFound
		}
		if err != nil {
			return "", err
		}
	}
	if err := TxManifestLock(ctx, tx, repo, digest); err != nil {
		return "", err
	}
	// Refuse while a pull lease protects the manifest.
	n, err := activeLeaseCount(ctx, tx, "manifest", repo, digest)
	if err != nil {
		return "", err
	}
	if n > 0 {
		return "", ErrLeased
	}
	ct, err := tx.Exec(ctx, "DELETE FROM manifests WHERE repo=$1 AND digest=$2", repo, digest)
	if err != nil {
		return "", err
	}
	if ct.RowsAffected() == 0 {
		return "", ErrNotFound
	}
	if err := tx.Commit(ctx); err != nil {
		return "", err
	}
	return digest, nil
}

// ErrLeased indicates an active read lease blocks the operation.
var ErrLeased = errors.New("object is protected by an active read lease")

func (s *Store) DeleteBlobAPI(ctx context.Context, digest string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := TxPublishLock(ctx, tx); err != nil {
		return err
	}
	if err := TxBlobLock(ctx, tx, digest); err != nil {
		return err
	}
	var exists bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", digest).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return ErrNotFound
	}
	n, err := activeLeaseCount(ctx, tx, "blob", "", digest)
	if err != nil {
		return err
	}
	if n > 0 {
		return ErrLeased
	}
	referenced, err := blobReferencedInTx(ctx, tx, digest)
	if err != nil {
		return err
	}
	if referenced {
		return ErrReferenced
	}
	if err := s.DeleteBlobRow(ctx, tx, digest); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ErrReferenced means the blob is still referenced by a manifest.
var ErrReferenced = errors.New("blob is referenced by a manifest")

func (s *Store) ListTags(ctx context.Context, repo string) ([]string, error) {
	rows, err := s.pool.Query(ctx, "SELECT name FROM tags WHERE repo=$1 ORDER BY name", repo)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

type AllManifest struct {
	Repo   string
	Digest string
}

func (s *Store) ListAllManifests(ctx context.Context) ([]AllManifest, error) {
	rows, err := s.pool.Query(ctx, "SELECT repo, digest FROM manifests ORDER BY repo, digest")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AllManifest
	for rows.Next() {
		var m AllManifest
		if err := rows.Scan(&m.Repo, &m.Digest); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// Upload sessions
// ---------------------------------------------------------------------------

type Upload struct {
	ID        string
	Repo      string
	Name      string
	Offset    int64
	StartedAt time.Time
	UpdatedAt time.Time
}

func (s *Store) CreateUpload(ctx context.Context, id, repo, name string) error {
	_, err := s.pool.Exec(ctx,
		"INSERT INTO uploads(id, repo, name) VALUES ($1,$2,$3) ON CONFLICT (id) DO NOTHING",
		id, repo, name)
	return err
}

func (s *Store) GetUpload(ctx context.Context, id string) (*Upload, error) {
	var u Upload
	err := s.pool.QueryRow(ctx,
		"SELECT id, repo, name, offset_bytes, started_at, updated_at FROM uploads WHERE id=$1", id).
		Scan(&u.ID, &u.Repo, &u.Name, &u.Offset, &u.StartedAt, &u.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &u, nil
}

func (s *Store) SetUploadOffset(ctx context.Context, id string, offset int64) error {
	ct, err := s.pool.Exec(ctx, "UPDATE uploads SET offset_bytes=$2, updated_at=now() WHERE id=$1", id, offset)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) DeleteUpload(ctx context.Context, id string) error {
	_, err := s.pool.Exec(ctx, "DELETE FROM uploads WHERE id=$1", id)
	return err
}

// ExpiredUpload is an upload session older than the cutoff (orphan candidate).
type ExpiredUpload struct {
	ID        string
	Repo      string
	Name      string
	StartedAt time.Time
}

func (s *Store) ListUploadsOlderThan(ctx context.Context, t time.Time) ([]ExpiredUpload, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT id, repo, name, started_at FROM uploads WHERE started_at < $1 ORDER BY id", t)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ExpiredUpload
	for rows.Next() {
		var u ExpiredUpload
		if err := rows.Scan(&u.ID, &u.Repo, &u.Name, &u.StartedAt); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// ListAllUploads returns every upload session regardless of age.
func (s *Store) ListAllUploads(ctx context.Context) ([]ExpiredUpload, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT id, repo, name, started_at FROM uploads ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ExpiredUpload
	for rows.Next() {
		var u ExpiredUpload
		if err := rows.Scan(&u.ID, &u.Repo, &u.Name, &u.StartedAt); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// Leases
// ---------------------------------------------------------------------------

func (s *Store) AddLease(ctx context.Context, id, repo, kind, digest string, expiresAt time.Time) error {
	_, err := s.pool.Exec(ctx,
		"INSERT INTO leases(id, repo, kind, digest, expires_at) VALUES ($1,$2,$3,$4,$5)",
		id, repo, kind, digest, expiresAt)
	return err
}

func (s *Store) DeleteLease(ctx context.Context, id string) error {
	_, err := s.pool.Exec(ctx, "DELETE FROM leases WHERE id=$1", id)
	return err
}

func (s *Store) RenewLease(ctx context.Context, id string, expiresAt time.Time) (kind, repo, digest string, err error) {
	err = s.pool.QueryRow(ctx,
		"UPDATE leases SET expires_at=$2 WHERE id=$1 RETURNING kind, repo, digest",
		id, expiresAt).Scan(&kind, &repo, &digest)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", "", ErrNotFound
	}
	return kind, repo, digest, err
}

func (s *Store) GetLease(ctx context.Context, id string) (repo, kind, digest string, expiresAt time.Time, err error) {
	err = s.pool.QueryRow(ctx,
		"SELECT repo, kind, digest, expires_at FROM leases WHERE id=$1", id).
		Scan(&repo, &kind, &digest, &expiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", "", time.Time{}, ErrNotFound
	}
	return repo, kind, digest, expiresAt, err
}

func activeLeaseCount(ctx context.Context, tx pgx.Tx, kind, repo, digest string) (int, error) {
	var n int
	var err error
	if kind == "blob" {
		err = tx.QueryRow(ctx,
			"SELECT count(*) FROM leases WHERE kind='blob' AND digest=$1 AND expires_at > now()",
			digest).Scan(&n)
	} else {
		err = tx.QueryRow(ctx,
			"SELECT count(*) FROM leases WHERE kind='manifest' AND repo=$1 AND digest=$2 AND expires_at > now()",
			repo, digest).Scan(&n)
	}
	return n, err
}

// ---------------------------------------------------------------------------
// Reachability (the GC graph query)
// ---------------------------------------------------------------------------

// Reachability rows: kind 'manifest' (repo, digest) or 'blob' (repo=”).
type ReachRow struct {
	Kind   string
	Repo   string
	Digest string
	Depth  int
}

// ReachableSet computes, inside tx, every object reachable from:
//   - all tags (-> manifests), walking manifest edges recursively
//   - every currently active (unexpired) read lease
//
// Returns a set encoded "blob:<digest>" / "manifest:<repo>:<digest>".
func ReachableSet(ctx context.Context, tx pgx.Tx) (map[string]ReachRow, error) {
	// A single recursive CTE over typed nodes. Tags and leases are roots;
	// manifest_refs provide edges. CYCLE guard handles pathological index
	// subject cycles. Depth is capped as defense in depth.
	const q = `
		WITH RECURSIVE reach(kind, repo, digest, depth) AS (
			SELECT kind, repo, digest, 0 FROM (
				SELECT 'manifest' AS kind, t.repo AS repo, t.digest AS digest FROM tags t
				UNION
				SELECT 'manifest', l.repo, l.digest FROM leases l
				WHERE l.kind='manifest' AND l.expires_at > now()
				UNION
				SELECT 'blob', '', l.digest FROM leases l
				WHERE l.kind='blob' AND l.expires_at > now()
			) AS roots
			UNION
			SELECT CASE WHEN r.child_kind='blob' THEN 'blob' ELSE 'manifest' END,
			       CASE WHEN r.child_kind='blob' THEN '' ELSE r.repo END,
			       r.child_digest,
			       x.depth + 1
			FROM reach x
			JOIN manifest_refs r
			  ON r.parent_digest = x.digest
			 AND (r.child_kind = 'blob' OR r.repo = x.repo)
			WHERE x.depth < 100
		)
		CYCLE kind, repo, digest SET is_cycle USING path
		SELECT DISTINCT kind, repo, digest, min(depth)
		FROM reach WHERE NOT is_cycle
		GROUP BY kind, repo, digest`
	rows, err := tx.Query(ctx, q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	set := map[string]ReachRow{}
	for rows.Next() {
		var r ReachRow
		if err := rows.Scan(&r.Kind, &r.Repo, &r.Digest, &r.Depth); err != nil {
			return nil, err
		}
		set[SetKey(r.Kind, r.Repo, r.Digest)] = r
	}
	return set, rows.Err()
}

// SetKey is the canonical map key encoding.
func SetKey(kind, repo, digest string) string { return kind + "|" + repo + "|" + digest }

func blobReferencedInTx(ctx context.Context, tx pgx.Tx, digest string) (bool, error) {
	var ok bool
	err := tx.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM manifest_refs WHERE child_kind='blob' AND child_digest=$1)`,
		digest).Scan(&ok)
	return ok, err
}

// BlobReferencingManifests returns (repo, parent digest) edges into a blob,
// used to build human-readable retain reasons.
func (s *Store) BlobReferencingManifests(ctx context.Context, digest string) ([]struct{ Repo, Digest string }, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT DISTINCT repo, parent_digest FROM manifest_refs
		 WHERE child_kind='blob' AND child_digest=$1 ORDER BY repo, parent_digest`, digest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []struct{ Repo, Digest string }
	for rows.Next() {
		var v struct{ Repo, Digest string }
		if err := rows.Scan(&v.Repo, &v.Digest); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// TagPointingAt returns tag names in repo pointing directly at the manifest.
func (s *Store) TagPointingAt(ctx context.Context, repo, digest string) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT name FROM tags WHERE repo=$1 AND digest=$2 ORDER BY name", repo, digest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

// IncomingManifestRefs returns (repo, digest) of manifests referencing the
// given manifest (index parents / referrers).
func (s *Store) IncomingManifestRefs(ctx context.Context, repo, digest string) ([]struct{ Repo, Digest string }, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT DISTINCT repo, parent_digest FROM manifest_refs
		 WHERE child_kind='manifest' AND repo=$1 AND child_digest=$2`, repo, digest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []struct{ Repo, Digest string }
	for rows.Next() {
		var v struct{ Repo, Digest string }
		if err := rows.Scan(&v.Repo, &v.Digest); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// ActiveLeasesForBlob returns lease ids currently pinning a blob.
func (s *Store) ActiveLeasesForBlob(ctx context.Context, digest string) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT id FROM leases WHERE kind='blob' AND digest=$1 AND expires_at > now() ORDER BY id", digest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

func (s *Store) ActiveLeasesForManifest(ctx context.Context, repo, digest string) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT id FROM leases WHERE kind='manifest' AND repo=$1 AND digest=$2 AND expires_at > now() ORDER BY id",
		repo, digest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// AcquireGCxid returns pg_current_xact_id() as int64 for the mark transaction.
func AcquireGCxid(ctx context.Context, tx pgx.Tx) (int64, time.Time, error) {
	var xid int64
	var ts time.Time
	err := tx.QueryRow(ctx, "SELECT pg_current_xact_id()::text::bigint, now()").Scan(&xid, &ts)
	return xid, ts, err
}

// RunGCTryLock is a session lock ensuring only one GC run per process cluster.
func (s *Store) TryGCLock(ctx context.Context, conn *pgxpool.Conn) (bool, error) {
	var ok bool
	err := conn.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", gcRunLockKey()).Scan(&ok)
	return ok, err
}

func (s *Store) ReleaseGCLock(ctx context.Context, conn *pgxpool.Conn) error {
	_, err := conn.Exec(ctx, "SELECT pg_advisory_unlock($1)", gcRunLockKey())
	return err
}
