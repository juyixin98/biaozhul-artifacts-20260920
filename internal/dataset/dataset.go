// Package dataset implements chunked dataset uploads, crash-safe merge/publish
// and garbage collection of content-addressed blobs.
//
// Upload lifecycle:
//
//	uploading  --(POST publish, all chunks received & manifest valid)--> publishing
//	publishing --(merge verified, hash matches, blob adopted)----------> ready
//
// A dataset is only ever readable in "ready". A crash during merge leaves the
// row in "publishing"; RecoverAtStartup resets such rows to "uploading" and
// wipes the temp directory, so no half-written file is ever served.
package dataset

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"time"

	sq "github.com/Masterminds/squirrel"
	"github.com/jmoiron/sqlx"
	"github.com/lib/pq"

	"github.com/synapticgo/synapticgo/internal/httpx"
	"github.com/synapticgo/synapticgo/internal/storage"
)

const (
	statusUploading  = "uploading"
	statusPublishing = "publishing"
	statusReady      = "ready"
	blobDeleting     = "deleting"
	blobReady        = "ready"
)

// Hooks inject failures for crash-recovery tests.
type Hooks struct {
	// AfterMergeWrite runs after the merged temp file has been written and
	// verified but before the publish transaction commits. Returning an error
	// aborts the publish exactly like a process failure at that point.
	AfterMergeWrite func(datasetID int64) error
}

// Service contains the dataset upload/publish/GC logic.
type Service struct {
	DB    *sqlx.DB
	Store *storage.Store
	Hooks Hooks
}

// ChunkSpec is one declared chunk in the creation request.
type ChunkSpec struct {
	Idx    int    `json:"idx"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// CreateRequest starts an upload session.
type CreateRequest struct {
	Name        string      `json:"name"`
	TotalSize   int64       `json:"total_size"`
	ChunkSize   int64       `json:"chunk_size"`
	WholeSHA256 string      `json:"whole_sha256"`
	Chunks      []ChunkSpec `json:"chunks"`
}

// ChunkView is the per-chunk status in API responses.
type ChunkView struct {
	Idx      int    `json:"idx"`
	Size     int64  `json:"size"`
	Offset   int64  `json:"offset"`
	SHA256   string `json:"sha256"`
	Received bool   `json:"received"`
}

// View is the dataset representation returned by the API.
type View struct {
	ID           int64       `json:"id" db:"id"`
	OwnerID      int64       `json:"owner_id" db:"owner_id"`
	Name         string      `json:"name" db:"name"`
	Status       string      `json:"status" db:"status"`
	TotalSize    int64       `json:"total_size" db:"total_size"`
	ChunkSize    int64       `json:"chunk_size" db:"chunk_size"`
	WantWholeSHA string      `json:"whole_sha256" db:"want_whole_sha256"`
	ReadyAt      *time.Time  `json:"ready_at,omitempty" db:"ready_at"`
	CreatedAt    time.Time   `json:"created_at" db:"created_at"`
	Chunks       []ChunkView `json:"chunks,omitempty"`
}

type datasetRow struct {
	ID           int64      `db:"id"`
	OwnerID      int64      `db:"owner_id"`
	Name         string     `db:"name"`
	Status       string     `db:"status"`
	TotalSize    int64      `db:"total_size"`
	ChunkSize    int64      `db:"chunk_size"`
	WantWholeSHA string     `db:"want_whole_sha256"`
	WholeSHA     *string    `db:"whole_sha256"`
	ReadyAt      *time.Time `db:"ready_at"`
	CreatedAt    time.Time  `db:"created_at"`
}

type chunkRow struct {
	Idx     int     `db:"idx"`
	Size    int64   `db:"size"`
	Offset  int64   `db:"chunk_offset"`
	WantSHA string  `db:"want_sha256"`
	BlobSHA *string `db:"blob_sha256"`
}

var psql = sq.StatementBuilder.PlaceholderFormat(sq.Dollar)

// validateManifest checks the creation request and returns rows with
// server-computed offsets.
func validateManifest(req *CreateRequest) ([]chunkRow, error) {
	if len(req.Name) < 1 || len(req.Name) > 200 {
		return nil, httpx.ErrBadRequest("name must be 1..200 characters")
	}
	if req.ChunkSize <= 0 {
		return nil, httpx.ErrBadRequest("chunk_size must be positive")
	}
	if req.TotalSize <= 0 {
		return nil, httpx.ErrBadRequest("total_size must be positive")
	}
	if !storage.IsSHA256Hex(req.WholeSHA256) {
		return nil, httpx.ErrBadRequest("whole_sha256 must be 64 lowercase hex chars")
	}
	n := len(req.Chunks)
	if n == 0 {
		return nil, httpx.ErrBadRequest("at least one chunk is required")
	}
	byIdx := make(map[int]ChunkSpec, n)
	for _, c := range req.Chunks {
		if c.Idx < 0 || c.Idx >= n {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("chunk idx %d out of range [0,%d)", c.Idx, n))
		}
		if _, dup := byIdx[c.Idx]; dup {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("duplicate chunk idx %d", c.Idx))
		}
		if c.Size <= 0 {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("chunk %d size must be positive", c.Idx))
		}
		if c.Size > req.ChunkSize {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("chunk %d larger than chunk_size", c.Idx))
		}
		if !storage.IsSHA256Hex(c.SHA256) {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("chunk %d sha256 must be 64 lowercase hex chars", c.Idx))
		}
		byIdx[c.Idx] = c
	}
	rows := make([]chunkRow, n)
	var offset int64
	for i := 0; i < n; i++ {
		c := byIdx[i]
		if i < n-1 && c.Size != req.ChunkSize {
			return nil, httpx.ErrBadRequest(fmt.Sprintf("chunk %d must be exactly chunk_size", i))
		}
		rows[i] = chunkRow{Idx: i, Size: c.Size, Offset: offset, WantSHA: c.SHA256}
		offset += c.Size
	}
	if offset != req.TotalSize {
		return nil, httpx.ErrBadRequest("chunk sizes do not tile total_size")
	}
	return rows, nil
}

// Create records the upload manifest. No bytes have arrived yet.
func (s *Service) Create(ctx context.Context, ownerID int64, req *CreateRequest) (*View, error) {
	rows, err := validateManifest(req)
	if err != nil {
		return nil, err
	}
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	var id int64
	q := `INSERT INTO datasets(owner_id, name, total_size, chunk_size, want_whole_sha256)
	      VALUES ($1,$2,$3,$4,$5) RETURNING id`
	if err := tx.GetContext(ctx, &id, q, ownerID, req.Name, req.TotalSize, req.ChunkSize, req.WholeSHA256); err != nil {
		var pqErr *pq.Error
		if errors.As(err, &pqErr) && pqErr.Code == "23505" {
			return nil, httpx.ErrConflict("dataset name already exists for this user")
		}
		return nil, err
	}
	for _, r := range rows {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO dataset_chunks(dataset_id, idx, size, chunk_offset, want_sha256)
			VALUES ($1,$2,$3,$4,$5)`, id, r.Idx, r.Size, r.Offset, r.WantSHA); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.getOwned(ctx, ownerID, id)
}

// getOwned loads a dataset that must belong to ownerID; foreign datasets and
// missing datasets both look like 404 so existence is not leaked.
func (s *Service) getOwned(ctx context.Context, ownerID, id int64) (*View, error) {
	var ds datasetRow
	err := s.DB.GetContext(ctx, &ds, `
		SELECT id, owner_id, name, status, total_size, chunk_size,
		       want_whole_sha256, whole_sha256, ready_at, created_at
		FROM datasets WHERE id = $1 AND owner_id = $2`, id, ownerID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, httpx.ErrNotFound("dataset not found")
		}
		return nil, err
	}
	v := rowToView(&ds)
	var chunks []chunkRow
	if err := s.DB.SelectContext(ctx, &chunks, `
		SELECT idx, size, chunk_offset, want_sha256, blob_sha256
		FROM dataset_chunks WHERE dataset_id = $1 ORDER BY idx`, id); err != nil {
		return nil, err
	}
	v.Chunks = make([]ChunkView, len(chunks))
	for i, c := range chunks {
		v.Chunks[i] = ChunkView{
			Idx: c.Idx, Size: c.Size, Offset: c.Offset,
			SHA256: c.WantSHA, Received: c.BlobSHA != nil,
		}
	}
	return v, nil
}

// Get returns one owner-scoped dataset.
func (s *Service) Get(ctx context.Context, ownerID, id int64) (*View, error) {
	return s.getOwned(ctx, ownerID, id)
}

// List returns the caller's datasets, newest first.
func (s *Service) List(ctx context.Context, ownerID int64, limit int) ([]View, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var rows []datasetRow
	if err := s.DB.SelectContext(ctx, &rows, `
		SELECT id, owner_id, name, status, total_size, chunk_size,
		       want_whole_sha256, whole_sha256, ready_at, created_at
		FROM datasets WHERE owner_id = $1
		ORDER BY id DESC LIMIT $2`, ownerID, limit); err != nil {
		return nil, err
	}
	out := make([]View, len(rows))
	for i := range rows {
		out[i] = *rowToView(&rows[i])
	}
	return out, nil
}

func rowToView(d *datasetRow) *View {
	return &View{
		ID: d.ID, OwnerID: d.OwnerID, Name: d.Name, Status: d.Status,
		TotalSize: d.TotalSize, ChunkSize: d.ChunkSize,
		WantWholeSHA: d.WantWholeSHA, ReadyAt: d.ReadyAt, CreatedAt: d.CreatedAt,
	}
}

// UploadChunk stores one chunk from body. Re-uploading the same bytes for the
// same position is idempotent; different bytes are rejected as a conflict.
func (s *Service) UploadChunk(ctx context.Context, ownerID, datasetID int64, idx int, body io.Reader, maxBytes int64) error {
	tmpName := fmt.Sprintf("chunk-%d-%d-%d", datasetID, idx, time.Now().UnixNano())
	f, err := s.Store.CreateTemp(tmpName)
	if err != nil {
		return err
	}
	sha, n, err := storage.CopyAndHash(f, io.LimitReader(body, maxBytes+1))
	if err != nil {
		_ = f.Close()
		_ = s.Store.RemoveTemp(tmpName)
		return err
	}
	if err := f.Close(); err != nil {
		_ = s.Store.RemoveTemp(tmpName)
		return err
	}
	if n > maxBytes {
		_ = s.Store.RemoveTemp(tmpName)
		return httpx.ErrUnprocess(fmt.Sprintf("chunk exceeds %d byte limit", maxBytes))
	}

	err = s.withAdoptedBlob(sha, n, s.Store.TempPath(tmpName), func(tx *sqlx.Tx) error {
		var ds datasetRow
		if err := tx.GetContext(ctx, &ds, `
			SELECT id, owner_id, name, status, total_size, chunk_size,
			       want_whole_sha256, whole_sha256, ready_at, created_at
			FROM datasets WHERE id = $1 FOR UPDATE`, datasetID); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return httpx.ErrNotFound("dataset not found")
			}
			return err
		}
		if ds.OwnerID != ownerID {
			return httpx.ErrNotFound("dataset not found")
		}
		var c chunkRow
		err := tx.GetContext(ctx, &c, `
			SELECT idx, size, chunk_offset, want_sha256, blob_sha256
			FROM dataset_chunks WHERE dataset_id = $1 AND idx = $2 FOR UPDATE`,
			datasetID, idx)
		if err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return httpx.ErrNotFound("chunk index not part of manifest")
			}
			return err
		}
		if n != c.Size {
			return httpx.ErrUnprocess(fmt.Sprintf("chunk size %d does not match declared %d", n, c.Size))
		}
		if sha != c.WantSHA {
			return httpx.ErrConflict("retransmitted chunk content differs from the manifest")
		}
		// Once the chunk (and dataset) is settled, a byte-identical retransmit
		// stays idempotent even after publishing; clients can safely retry.
		if c.BlobSHA != nil {
			if *c.BlobSHA != sha {
				return httpx.ErrConflict("retransmitted chunk content differs from stored chunk")
			}
			return nil // identical retransmit: idempotent
		}
		if ds.Status != statusUploading {
			return httpx.ErrConflict(fmt.Sprintf("dataset is %s; chunk upload closed", ds.Status))
		}
		if err := adoptInTx(tx, sha, n); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx,
			`UPDATE dataset_chunks SET blob_sha256 = $1 WHERE dataset_id = $2 AND idx = $3`,
			sha, datasetID, idx)
		return err
	})
	if err != nil {
		_ = s.Store.RemoveTemp(tmpName)
		return err
	}
	return s.Store.RemoveTemp(tmpName)
}

// Publish merges received chunks into the final blob and flips the dataset to
// ready. It refuses when chunks are missing or the merged content does not
// match the declared whole-file hash.
func (s *Service) Publish(ctx context.Context, ownerID, datasetID int64) (*View, error) {
	// Phase 1: claim the dataset under a row lock and validate the manifest.
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	var ds datasetRow
	err = tx.GetContext(ctx, &ds, `
		SELECT id, owner_id, name, status, total_size, chunk_size,
		       want_whole_sha256, whole_sha256, ready_at, created_at
		FROM datasets WHERE id = $1 FOR UPDATE`, datasetID)
	if err != nil {
		_ = tx.Rollback()
		if errors.Is(err, sql.ErrNoRows) {
			return nil, httpx.ErrNotFound("dataset not found")
		}
		return nil, err
	}
	if ds.OwnerID != ownerID {
		_ = tx.Rollback()
		return nil, httpx.ErrNotFound("dataset not found")
	}
	switch ds.Status {
	case statusReady:
		_ = tx.Rollback()
		return s.getOwned(ctx, ownerID, datasetID) // publish is idempotent
	case statusPublishing:
		_ = tx.Rollback()
		return nil, httpx.ErrConflict("merge already in progress; retry after recovery")
	}
	chunks, err := lockedChunks(ctx, tx, datasetID)
	if err != nil {
		_ = tx.Rollback()
		return nil, err
	}
	if err := verifyComplete(chunks, ds.TotalSize); err != nil {
		_ = tx.Rollback()
		return nil, err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE datasets SET status = $1 WHERE id = $2 AND status = $3`,
		statusPublishing, datasetID, statusUploading); err != nil {
		_ = tx.Rollback()
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}

	// Phase 2: stream chunks in order into a temp file and verify the merge.
	tmpName := fmt.Sprintf("merge-%d-%d", datasetID, time.Now().UnixNano())
	merged, sha, size, err := s.mergeToTemp(chunks, tmpName)
	if err != nil {
		_ = s.Store.RemoveTemp(tmpName)
		return nil, err
	}
	if size != ds.TotalSize || sha != ds.WantWholeSHA {
		_ = s.Store.RemoveTemp(tmpName)
		_ = merged.Close()
		return nil, httpx.ErrUnprocess("merged content does not match declared size/sha256")
	}
	if s.Hooks.AfterMergeWrite != nil {
		if err := s.Hooks.AfterMergeWrite(datasetID); err != nil {
			_ = s.Store.RemoveTemp(tmpName)
			return nil, fmt.Errorf("merge interrupted by failure hook: %w", err)
		}
	}
	if err := merged.Close(); err != nil {
		_ = s.Store.RemoveTemp(tmpName)
		return nil, err
	}

	// Phase 3: adopt the merged blob and publish atomically.
	err = s.withAdoptedBlob(sha, size, s.Store.TempPath(tmpName), func(tx *sqlx.Tx) error {
		var again datasetRow
		if err := tx.GetContext(ctx, &again, `
			SELECT id, owner_id, name, status, total_size, chunk_size,
			       want_whole_sha256, whole_sha256, ready_at, created_at
			FROM datasets WHERE id = $1 FOR UPDATE`, datasetID); err != nil {
			return err
		}
		if again.OwnerID != ownerID {
			return httpx.ErrNotFound("dataset not found")
		}
		if again.Status != statusPublishing {
			return httpx.ErrConflict("dataset no longer publishing")
		}
		if err := adoptInTx(tx, sha, size); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `
			UPDATE datasets
			   SET status = $1, whole_sha256 = $2, ready_at = now()
			 WHERE id = $3 AND status = $4`,
			statusReady, sha, datasetID, statusPublishing)
		return err
	})
	if err != nil {
		_ = s.Store.RemoveTemp(tmpName)
		return nil, err
	}
	if err := s.Store.RemoveTemp(tmpName); err != nil {
		log.Printf("warning: remove merge temp %s: %v", tmpName, err)
	}
	return s.getOwned(ctx, ownerID, datasetID)
}

// Delete removes a dataset row. Referencing model versions block the delete;
// chunk rows cascade and blob refcounts drop via triggers, leaving actual
// file cleanup to GarbageCollect.
func (s *Service) Delete(ctx context.Context, ownerID, datasetID int64) error {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	var ds datasetRow
	err = tx.GetContext(ctx, &ds, `
		SELECT id, owner_id, name, status, total_size, chunk_size,
		       want_whole_sha256, whole_sha256, ready_at, created_at
		FROM datasets WHERE id = $1 FOR UPDATE`, datasetID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return httpx.ErrNotFound("dataset not found")
		}
		return err
	}
	if ds.OwnerID != ownerID {
		return httpx.ErrNotFound("dataset not found")
	}
	var refs int
	if err := tx.GetContext(ctx, &refs, `
		SELECT count(*) FROM model_versions
		 WHERE owner_id = $1
		   AND (train_dataset_sha = $2 OR eval_dataset_sha = $2)`,
		ownerID, ds.WantWholeSHA); err != nil {
		return err
	}
	if refs > 0 {
		return httpx.ErrConflict(fmt.Sprintf("%d model version(s) reference this dataset content", refs))
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM datasets WHERE id = $1`, datasetID); err != nil {
		return err
	}
	return tx.Commit()
}

// OpenContent returns the verified bytes of a ready, owner-scoped dataset.
func (s *Service) OpenContent(ctx context.Context, ownerID, datasetID int64) (raw []byte, sha string, err error) {
	var ds datasetRow
	err = s.DB.GetContext(ctx, &ds, `
		SELECT id, owner_id, name, status, total_size, chunk_size,
		       want_whole_sha256, whole_sha256, ready_at, created_at
		FROM datasets WHERE id = $1 AND owner_id = $2`, datasetID, ownerID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, "", httpx.ErrNotFound("dataset not found")
		}
		return nil, "", err
	}
	if ds.Status != statusReady || ds.WholeSHA == nil {
		return nil, "", httpx.ErrConflict("dataset is not published")
	}
	raw, err = os.ReadFile(s.Store.BlobPath(*ds.WholeSHA))
	if err != nil {
		return nil, "", fmt.Errorf("read published blob: %w", err)
	}
	return raw, *ds.WholeSHA, nil
}

// ReadySHA returns the whole-file hash of a ready dataset owned by ownerID.
func (s *Service) ReadySHA(ctx context.Context, ownerID, datasetID int64) (string, error) {
	var sha string
	err := s.DB.GetContext(ctx, &sha, `
		SELECT want_whole_sha256 FROM datasets
		 WHERE id = $1 AND owner_id = $2 AND status = $3`,
		datasetID, ownerID, statusReady)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", httpx.ErrNotFound("ready dataset not found")
		}
		return "", err
	}
	return sha, nil
}

func lockedChunks(ctx context.Context, tx *sqlx.Tx, datasetID int64) ([]chunkRow, error) {
	var chunks []chunkRow
	if err := tx.SelectContext(ctx, &chunks, `
		SELECT idx, size, chunk_offset, want_sha256, blob_sha256
		FROM dataset_chunks WHERE dataset_id = $1 ORDER BY idx FOR UPDATE`,
		datasetID); err != nil {
		return nil, err
	}
	return chunks, nil
}

func verifyComplete(chunks []chunkRow, totalSize int64) error {
	if len(chunks) == 0 {
		return httpx.ErrUnprocess("manifest has no chunks")
	}
	var offset int64
	for i, c := range chunks {
		if c.Idx != i {
			return httpx.ErrUnprocess("chunk indices are not contiguous")
		}
		if c.Offset != offset {
			return httpx.ErrUnprocess(fmt.Sprintf("chunk %d offset mismatch", i))
		}
		if c.BlobSHA == nil {
			return httpx.ErrUnprocess(fmt.Sprintf("chunk %d is missing; upload cannot be published", i))
		}
		if *c.BlobSHA != c.WantSHA {
			return httpx.ErrUnprocess(fmt.Sprintf("chunk %d stored hash differs from manifest", i))
		}
		offset += c.Size
	}
	if offset != totalSize {
		return httpx.ErrUnprocess("received chunks do not tile total_size")
	}
	return nil
}

func (s *Service) mergeToTemp(chunks []chunkRow, tmpName string) (*os.File, string, int64, error) {
	out, err := s.Store.CreateTemp(tmpName)
	if err != nil {
		return nil, "", 0, err
	}
	hasher := newSHA256Writer(out)
	var total int64
	for _, c := range chunks {
		path := s.Store.BlobPath(*c.BlobSHA)
		f, err := os.Open(path)
		if err != nil {
			_ = out.Close()
			return nil, "", 0, fmt.Errorf("open chunk %d blob: %w (storage inconsistent)", c.Idx, err)
		}
		info, err := f.Stat()
		if err == nil && info.Size() != c.Size {
			_ = f.Close()
			_ = out.Close()
			return nil, "", 0, httpx.ErrInternal(fmt.Sprintf("chunk %d size on disk differs from manifest", c.Idx))
		}
		n, err := io.CopyN(hasher, f, c.Size)
		_ = f.Close()
		if err != nil {
			_ = out.Close()
			return nil, "", 0, fmt.Errorf("merge chunk %d: %w", c.Idx, err)
		}
		total += n
	}
	sha := hasher.checksum()
	return out, sha, total, nil
}

// errBlobDeleting means the blob row is mid-garbage-collection; the caller
// retries after the GC finishes removing it.
var errBlobDeleting = errors.New("blob is being garbage collected")

// adoptInTx inserts the blob row if absent or takes its lock if present.
// The transaction holds the blob advisory lock, so it is mutually exclusive
// with garbage collection. Must run before any row references the blob, so
// reference and blob row commit together. The hard link into the blob
// namespace is created by the caller while this same transaction/lock is
// held, guaranteeing that a committed reference always has a file.
func adoptInTx(tx *sqlx.Tx, sha string, size int64) error {
	if _, err := tx.Exec(`SELECT pg_advisory_xact_lock($1)`, blobLockKey); err != nil {
		return fmt.Errorf("acquire blob lock: %w", err)
	}
	var state string
	err := tx.QueryRowx(`
		INSERT INTO blobs(sha256, size) VALUES ($1, $2)
		ON CONFLICT (sha256) DO UPDATE SET state = blobs.state
		RETURNING state`, sha, size).Scan(&state)
	if err != nil {
		return fmt.Errorf("adopt blob: %w", err)
	}
	if state == blobDeleting {
		return errBlobDeleting
	}
	return nil
}

// withAdoptedBlob runs fn in a transaction that adopts the blob row while
// holding the blob advisory lock. The caller-supplied fn must itself create
// the hard link of tempPath into the blob namespace before inserting its
// reference (LinkLocked), so link + row + reference commit as one step.
//
// If GC has marked the blob 'deleting', the transaction aborts and retries:
// GC drops the row shortly, after which the next attempt re-creates both row
// and file from the temp upload.
func (s *Service) withAdoptedBlob(sha string, size int64, tempPath string, fn func(tx *sqlx.Tx) error) error {
	const maxAttempts = 40
	backoff := 5 * time.Millisecond
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		tx, err := s.DB.Beginx()
		if err != nil {
			return err
		}
		runErr := func() error {
			if err := adoptInTx(tx, sha, size); err != nil {
				return err
			}
			// Link while lock + transaction are held. Hard linking is
			// atomic and idempotent; GC cannot unlink concurrently.
			if _, err := s.Store.LinkIntoBlob(tempPath, sha); err != nil {
				return fmt.Errorf("link blob: %w", err)
			}
			return fn(tx)
		}()
		if runErr == nil {
			if runErr = tx.Commit(); runErr == nil {
				return nil
			}
		} else {
			_ = tx.Rollback()
		}
		if errors.Is(runErr, errBlobDeleting) {
			lastErr = runErr
			time.Sleep(backoff)
			if backoff < 250*time.Millisecond {
				backoff *= 2
			}
			continue
		}
		return runErr
	}
	return fmt.Errorf("blob %s busy in garbage collection for too long: %w", sha, lastErr)
}
