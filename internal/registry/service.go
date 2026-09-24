// Package registry is the transactional core of the local content-addressable
// image registry: blob publication, manifests, tags and read leases.
package registry

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"layer-gc/internal/digest"
	"layer-gc/internal/storage"
)

var (
	// ErrNotFound is a generic not-found for manifests/tags/blobs.
	ErrNotFound = errors.New("not found")
	// ErrDigestMismatch means staged content does not match the claimed digest.
	ErrDigestMismatch = errors.New("digest mismatch")
	// ErrLayerMissing means a manifest references a layer blob that has not
	// been pushed yet.
	ErrLayerMissing = errors.New("referenced layer blob is missing")
)

// Descriptor is a minimal OCI content descriptor.
type Descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
}

// Manifest mirrors the fields of an OCI image manifest that GC cares about.
// Only "layers" establish references that protect blobs from GC.
type Manifest struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType,omitempty"`
	Config        Descriptor   `json:"config"`
	Layers        []Descriptor `json:"layers"`
}

// Service wires the database and the on-disk content store together.
type Service struct {
	DB    *sql.DB
	Store *storage.Store
	Clock func() time.Time
}

func New(db *sql.DB, store *storage.Store) *Service {
	return &Service{DB: db, Store: store, Clock: time.Now}
}

// StartUpload opens a staged temp file and records it.
func (s *Service) StartUpload(ctx context.Context) (uploadID, tempPath string, err error) {
	u, err := s.Store.BeginUpload()
	if err != nil {
		return "", "", err
	}
	if _, err := s.DB.ExecContext(ctx,
		`INSERT INTO blob_uploads(upload_id, temp_path) VALUES ($1,$2)`,
		u.ID, u.Path()); err != nil {
		_ = s.Store.DiscardUpload(u.ID)
		return "", "", err
	}
	return u.ID, u.Path(), nil
}

// AppendUpload streams a chunk into the staged upload.
func (s *Service) AppendUpload(ctx context.Context, uploadID string, r io.Reader) (int64, error) {
	u, err := s.Store.OpenUpload(uploadID)
	if err != nil {
		return 0, ErrNotFound
	}
	defer u.Close()
	// Open in append mode through a separate file handle: reopen O_WRONLY|O_APPEND.
	return appendToFile(u.Path(), r)
}

// CommitUpload verifies the staged content digest, publishes it into the CAS,
// and registers the blob row — all before the caller can reference it.
// Returns ErrDigestMismatch on bad content; the temp file is then removed.
func (s *Service) CommitUpload(ctx context.Context, uploadID, claimedDigest string) error {
	if _, err := digest.Check(claimedDigest); err != nil {
		return err
	}
	var path string
	err := s.DB.QueryRowContext(ctx,
		`SELECT temp_path FROM blob_uploads WHERE upload_id=$1 AND completed=FALSE`,
		uploadID).Scan(&path)
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return err
	}

	u := storage.NewUpload(uploadID, path)
	size, err := s.Store.Verify(u, claimedDigest)
	if err != nil {
		// Digest failure: clean up bookkeeping and the staged file.
		_, _ = s.DB.ExecContext(ctx, `DELETE FROM blob_uploads WHERE upload_id=$1`, uploadID)
		return fmt.Errorf("%w: %v", ErrDigestMismatch, err)
	}

	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	// Register the blob first; concurrent identical commits conflict on the
	// PK and one wins, both are valid because content == digest.
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO blobs(digest, size_bytes) VALUES ($1,$2)
		 ON CONFLICT (digest) DO NOTHING`, claimedDigest, size); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE blob_uploads SET completed=TRUE WHERE upload_id=$1`, uploadID); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}

	// Move verified bytes into the CAS only after the DB commit.  If this
	// process crashes between commit and rename, a blob row may reference a
	// file that is still staged; the orphan reconciler reports such cases.
	if err := s.Store.Publish(storage.NewUpload(uploadID, path), claimedDigest); err != nil {
		return err
	}
	_, _ = s.DB.ExecContext(ctx, `DELETE FROM blob_uploads WHERE upload_id=$1`, uploadID)
	return nil
}

// AbortUpload deletes an upload's staged file and bookkeeping row.
func (s *Service) AbortUpload(ctx context.Context, uploadID string) error {
	if _, err := s.DB.ExecContext(ctx,
		`DELETE FROM blob_uploads WHERE upload_id=$1`, uploadID); err != nil {
		return err
	}
	return s.Store.DiscardUpload(uploadID)
}

// HasBlob reports whether a blob exists in DB and on disk.
func (s *Service) HasBlob(ctx context.Context, d string) (bool, error) {
	var ok bool
	err := s.DB.QueryRowContext(ctx,
		`SELECT TRUE FROM blobs WHERE digest=$1`, d).Scan(&ok)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return s.Store.Exists(d), nil
}

// OpenBlob opens a published blob for streaming.
func (s *Service) OpenBlob(ctx context.Context, d string) (io.ReadCloser, int64, error) {
	var size int64
	err := s.DB.QueryRowContext(ctx,
		`SELECT size_bytes FROM blobs WHERE digest=$1`, d).Scan(&size)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, 0, ErrNotFound
	}
	if err != nil {
		return nil, 0, err
	}
	f, err := s.Store.Open(d)
	if err != nil {
		return nil, 0, err
	}
	return f, size, nil
}

// PutManifest validates and stores a manifest: every layer descriptor must
// point at an existing blob.  Layers are inserted as reference edges.
func (s *Service) PutManifest(ctx context.Context, m Manifest, raw []byte) (string, error) {
	d := digest.FromBytes(raw)
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer tx.Rollback()

	for _, l := range m.Layers {
		if l.Digest != "" && !digest.Valid(l.Digest) {
			return "", fmt.Errorf("invalid layer digest %q", l.Digest)
		}
		var present bool
		if err := tx.QueryRowContext(ctx,
			`SELECT TRUE FROM blobs WHERE digest=$1 FOR SHARE`, l.Digest).Scan(&present); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return "", fmt.Errorf("%w: %s", ErrLayerMissing, l.Digest)
			}
			return "", err
		}
	}

	// The config descriptor names a blob too; it must exist and is also a GC
	// root edge so a manifest never points at a deleted config.
	if m.Config.Digest != "" {
		var present bool
		if err := tx.QueryRowContext(ctx,
			`SELECT TRUE FROM blobs WHERE digest=$1 FOR SHARE`, m.Config.Digest).Scan(&present); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return "", fmt.Errorf("%w: config %s", ErrLayerMissing, m.Config.Digest)
			}
			return "", err
		}
	}

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO manifests(digest, content_type, size_bytes, payload)
		 VALUES ($1,$2,$3,$4)
		 ON CONFLICT (digest) DO UPDATE SET payload=EXCLUDED.payload`,
		d, mediaType(m), len(raw), JSON(raw)); err != nil {
		return "", err
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM manifest_refs WHERE manifest_digest=$1`, d); err != nil {
		return "", err
	}
	if m.Config.Digest != "" {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO manifest_refs(manifest_digest, blob_digest, kind, ordinal)
			 VALUES ($1,$2,'config',0) ON CONFLICT DO NOTHING`, d, m.Config.Digest); err != nil {
			return "", err
		}
	}
	for i, l := range m.Layers {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO manifest_refs(manifest_digest, blob_digest, kind, ordinal)
			 VALUES ($1,$2,'layer',$3) ON CONFLICT DO NOTHING`, d, l.Digest, i); err != nil {
			return "", err
		}
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return d, nil
}

// Tag points repo:tag at a manifest.  Tag updates are the normal way a blob
// becomes reachable while a GC is already sweeping.
func (s *Service) Tag(ctx context.Context, repo, tag, manifestDigest string) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	// SHARE-lock every layer of the manifest so an in-flight sweeper holding
	// FOR UPDATE cannot delete it underneath this tag update.
	rows, err := tx.QueryContext(ctx,
		`SELECT blob_digest FROM manifest_refs WHERE manifest_digest=$1`, manifestDigest)
	if err != nil {
		return err
	}
	var layers []string
	for rows.Next() {
		var ld string
		if err := rows.Scan(&ld); err != nil {
			rows.Close()
			return err
		}
		layers = append(layers, ld)
	}
	rows.Close()
	for _, ld := range layers {
		if _, err := tx.ExecContext(ctx, `SELECT 1 FROM blobs WHERE digest=$1 FOR SHARE`, ld); err != nil {
			return err
		}
	}
	res, err := tx.ExecContext(ctx,
		`INSERT INTO tags(repo,tag,manifest_digest,updated_at)
		 VALUES ($1,$2,$3,$4)
		 ON CONFLICT (repo,tag) DO UPDATE SET manifest_digest=EXCLUDED.manifest_digest,
		                                      updated_at=EXCLUDED.updated_at`,
		repo, tag, manifestDigest, s.Clock().UTC())
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

// Untag removes a tag.  The manifest stays; its layers lose this root only.
func (s *Service) Untag(ctx context.Context, repo, tag string) error {
	res, err := s.DB.ExecContext(ctx,
		`DELETE FROM tags WHERE repo=$1 AND tag=$2`, repo, tag)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

// DeleteManifest removes a manifest and its layer reference edges.  All tags
// pointing at it must already be gone (RESTRICT); its layers become GC
// candidates if no other manifest references them.
func (s *Service) DeleteManifest(ctx context.Context, manifestDigest string) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	// Lock the layers FOR SHARE so a concurrent sweeper (FOR UPDATE) cannot
	// delete one while the reference edges are being torn down.
	rows, err := tx.QueryContext(ctx,
		`SELECT blob_digest FROM manifest_refs WHERE manifest_digest=$1`, manifestDigest)
	if err != nil {
		return err
	}
	var layers []string
	for rows.Next() {
		var ld string
		if err := rows.Scan(&ld); err != nil {
			rows.Close()
			return err
		}
		layers = append(layers, ld)
	}
	rows.Close()
	for _, ld := range layers {
		if _, err := tx.ExecContext(ctx, `SELECT 1 FROM blobs WHERE digest=$1 FOR SHARE`, ld); err != nil {
			return err
		}
	}
	res, err := tx.ExecContext(ctx, `DELETE FROM manifests WHERE digest=$1`, manifestDigest)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

// ResolveTag returns the manifest digest behind repo:tag.
func (s *Service) ResolveTag(ctx context.Context, repo, tag string) (string, error) {
	var d string
	err := s.DB.QueryRowContext(ctx,
		`SELECT manifest_digest FROM tags WHERE repo=$1 AND tag=$2`, repo, tag).Scan(&d)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	return d, err
}

// GetManifest returns the stored manifest and its digest.
func (s *Service) GetManifest(ctx context.Context, d string) (*Manifest, []byte, error) {
	var raw []byte
	err := s.DB.QueryRowContext(ctx,
		`SELECT payload FROM manifests WHERE digest=$1`, d).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, ErrNotFound
	}
	if err != nil {
		return nil, nil, err
	}
	var m Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, nil, err
	}
	return &m, raw, nil
}

// ManifestLayers returns the layer digests referenced by a manifest.
func (s *Service) ManifestLayers(ctx context.Context, manifestDigest string) ([]string, error) {
	rows, err := s.DB.QueryContext(ctx,
		`SELECT blob_digest FROM manifest_refs WHERE manifest_digest=$1 AND kind='layer' ORDER BY ordinal`,
		manifestDigest)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var d string
		if err := rows.Scan(&d); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// AcquireLease pins a set of blobs for holder until ttl, returning lease ids.
// Used when a pull starts streaming layer content.  Fails if any blob is gone.
func (s *Service) AcquireLease(ctx context.Context, holder string, ttl time.Duration, digests []string) ([]int64, error) {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	exp := s.Clock().Add(ttl).UTC()
	ids := make([]int64, 0, len(digests))
	for _, d := range digests {
		var id int64
		// FOR SHARE blocks a concurrent sweeper's FOR UPDATE long enough to
		// register the lease; if the blob was already deleted we get no row.
		err := tx.QueryRowContext(ctx,
			`INSERT INTO leases(blob_digest, holder, expires_at)
			 SELECT $1,$2,$3 WHERE EXISTS (SELECT 1 FROM blobs WHERE digest=$1 FOR SHARE)
			 RETURNING id`, d, holder, exp).Scan(&id)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, fmt.Errorf("%w: %s", ErrNotFound, d)
		}
		if err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return ids, nil
}

// ReleaseLease releases lease ids (pull finished normally).  Unknown ids are
// ignored so retries are safe.
func (s *Service) ReleaseLease(ctx context.Context, ids []int64) error {
	if len(ids) == 0 {
		return nil
	}
	_, err := s.DB.ExecContext(ctx, `DELETE FROM leases WHERE id = ANY($1)`, ids)
	return err
}

// JSON is a typed raw-JSON helper kept readable in call sites.
type JSON json.RawMessage

// Value implements driver.Valuer.
func (j JSON) Value() (interface{}, error) {
	if len(j) == 0 {
		return []byte("null"), nil
	}
	return []byte(j), nil
}

func mediaType(m Manifest) string {
	if m.MediaType != "" {
		return m.MediaType
	}
	return "application/vnd.oci.image.manifest.v1+json"
}

// appendToFile opens path append-only and streams r to it, fsyncing afterwards.
func appendToFile(path string, r io.Reader) (int64, error) {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return 0, ErrNotFound
		}
		return 0, err
	}
	defer f.Close()
	n, err := io.Copy(f, r)
	if err != nil {
		return n, err
	}
	return n, f.Sync()
}
