// Package dataset implements chunked dataset uploads and content-addressed
// publishing.
package dataset

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/models"
	"synapticgo/internal/store"
)

// Sentinel errors mapped to HTTP statuses by the handler layer.
var (
	ErrNotFound       = errors.New("dataset not found")
	ErrForbidden      = errors.New("not the dataset owner")
	ErrNotUploading   = errors.New("dataset is not accepting uploads")
	ErrChunkIndex     = errors.New("chunk index out of range")
	ErrChunkOffset    = errors.New("chunk offset does not match position")
	ErrChunkLength    = errors.New("chunk length exceeds final chunk")
	ErrDigestMismatch = errors.New("content digest does not match declared digest")
	ErrSizeMismatch   = errors.New("uploaded bytes do not match declared size")
	ErrMissingChunks  = errors.New("dataset has missing chunks")
	ErrWholeMismatch  = errors.New("assembled digest does not match declared whole digest")
	ErrInUse          = errors.New("dataset is referenced by model versions")
)

// FaultHooks are optional injection points used by tests to simulate a process
// crash at a precise point.
type FaultHooks struct {
	// AfterAssemble fires after the whole-file blob is assembled and verified,
	// before the metadata transaction commits.
	AfterAssemble func(datasetID int64) error
	// AfterPublishCommit fires after the publish transaction committed.
	AfterPublishCommit func(datasetID int64)
}

type Service struct {
	db  *sqlx.DB
	obj *store.ObjectService
	fs  *store.FileStore

	Faults FaultHooks
}

func NewService(db *sqlx.DB, obj *store.ObjectService, fs *store.FileStore) *Service {
	return &Service{db: db, obj: obj, fs: fs}
}

// Create registers a dataset shell. Every chunk except the last must be exactly
// chunkSize bytes; totalSize determines chunk_count and the final length.
func (s *Service) Create(ctx context.Context, ownerID int64, name string, totalSize int64, chunkSize int32, wholeDigest *string) (*models.Dataset, error) {
	if name == "" {
		return nil, fmt.Errorf("name required")
	}
	if totalSize < 0 {
		return nil, fmt.Errorf("total_size must be >= 0")
	}
	if chunkSize <= 0 {
		return nil, fmt.Errorf("chunk_size must be > 0")
	}
	if totalSize == 0 {
		return nil, fmt.Errorf("total_size must be > 0")
	}
	if wholeDigest != nil && !store.ValidateDigest(*wholeDigest) {
		return nil, fmt.Errorf("whole_digest must be 64 lowercase hex chars")
	}
	chunkCount := int32((totalSize + int64(chunkSize) - 1) / int64(chunkSize))

	var d models.Dataset
	err := s.db.GetContext(ctx, &d,
		`INSERT INTO datasets(owner_id, name, total_size, chunk_size, chunk_count, whole_digest)
		 VALUES ($1,$2,$3,$4,$5,$6)
		 RETURNING id, owner_id, name, total_size, chunk_size, chunk_count, whole_digest,
		           whole_object_id, status, created_at, published_at`,
		ownerID, name, totalSize, chunkSize, chunkCount, nilIfEmpty(wholeDigest))
	if err != nil {
		return nil, err
	}
	return &d, nil
}

func nilIfEmpty(s *string) any {
	if s == nil || *s == "" {
		return nil
	}
	return *s
}

// ExpectedChunk returns the offset and required length of a chunk index.
func ExpectedChunk(d *models.Dataset, index int32) (offset int64, length int32, err error) {
	if index < 0 || index >= d.ChunkCount {
		return 0, 0, ErrChunkIndex
	}
	offset = int64(index) * int64(d.ChunkSize)
	remaining := d.TotalSize - offset
	if remaining >= int64(d.ChunkSize) {
		length = d.ChunkSize
	} else {
		length = int32(remaining)
	}
	return offset, length, nil
}

// UploadChunk stores one chunk. It is idempotent: re-sending the identical
// chunk returns the existing record; sending the same index with different
// bytes (or bytes colliding with an existing object of a different digest
// declaration) is rejected. Chunks may arrive out of order.
func (s *Service) UploadChunk(ctx context.Context, callerID, datasetID int64, index int32, body io.Reader, declaredDigest string) (*models.Chunk, bool, error) {
	if !store.ValidateDigest(declaredDigest) {
		return nil, false, fmt.Errorf("digest must be 64 lowercase hex chars")
	}

	d, err := s.getOwned(ctx, datasetID, callerID)
	if err != nil {
		return nil, false, err
	}
	offset, expectedLen, err := ExpectedChunk(d, index)
	if err != nil {
		return nil, false, err
	}

	// Stream to staging first so the slow I/O happens outside the transaction.
	staging, n, actualDigest, err := s.fs.StagingFromReader(body)
	if err != nil {
		return nil, false, err
	}
	defer s.fs.RemoveStaging(staging)

	if n != int64(expectedLen) {
		return nil, false, fmt.Errorf("%w: got %d bytes, want %d", ErrSizeMismatch, n, expectedLen)
	}
	// Never trust the client: the declared digest must match the bytes.
	if actualDigest != declaredDigest {
		return nil, false, fmt.Errorf("%w: header declares %s but content hashes to %s",
			ErrDigestMismatch, declaredDigest, actualDigest)
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback()

	// Re-check status and the slot under the dataset row lock.
	var status string
	if err := tx.Get(&status, `SELECT status FROM datasets WHERE id = $1 FOR UPDATE`, d.ID); err != nil {
		return nil, false, err
	}
	if status != models.StatusUploading {
		return nil, false, ErrNotUploading
	}

	var existing models.Chunk
	err = tx.Get(&existing,
		`SELECT id, dataset_id, chunk_index, offset_bytes, length, digest, object_id, uploaded_at
		 FROM dataset_chunks WHERE dataset_id = $1 AND chunk_index = $2`, d.ID, index)
	switch {
	case err == nil:
		if existing.OffsetBytes != offset || existing.Length != expectedLen {
			return nil, false, fmt.Errorf("%w: stored slot geometry differs", ErrChunkOffset)
		}
		if existing.Digest == declaredDigest {
			return &existing, true, nil // idempotent retry
		}
		return nil, false, fmt.Errorf("%w: chunk %d already stored with a different digest", ErrDigestMismatch, index)
	case !errors.Is(err, sql.ErrNoRows):
		return nil, false, err
	}

	// Acquire bumps the refcount of an identical existing object or commits
	// the staged bytes under the declared digest. Its per-digest advisory lock
	// serializes this against releases to zero.
	objectID, err := s.obj.Acquire(tx, declaredDigest, n, staging)
	if err != nil {
		return nil, false, err
	}

	var c models.Chunk
	err = tx.Get(&c,
		`INSERT INTO dataset_chunks(dataset_id, chunk_index, offset_bytes, length, digest, object_id)
		 VALUES ($1,$2,$3,$4,$5,$6)
		 RETURNING id, dataset_id, chunk_index, offset_bytes, length, digest, object_id, uploaded_at`,
		d.ID, index, offset, expectedLen, declaredDigest, objectID)
	if err != nil {
		return nil, false, err
	}
	if err := tx.Commit(); err != nil {
		return nil, false, err
	}
	return &c, false, nil
}

// Publish assembles every chunk in order, verifies the whole-file digest, and
// atomically flips the dataset to ready. It uses a status compare-and-set so
// concurrent publishes serialize: exactly one proceeds, the rest get
// ErrNotUploading (already publishing/ready).
//
// The metadata flip, the whole-object refcount bump and per-chunk ref release
// happen in one transaction, so a crash at any point leaves the dataset
// resumable: nothing is ever marked ready without a committed, verified blob.
func (s *Service) Publish(ctx context.Context, callerID, datasetID int64) (*models.Dataset, string, error) {
	d, err := s.getOwned(ctx, datasetID, callerID)
	if err != nil {
		return nil, "", err
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()

	var status string
	if err := tx.Get(&status, `SELECT status FROM datasets WHERE id = $1 FOR UPDATE`, d.ID); err != nil {
		return nil, "", err
	}
	if status != models.StatusUploading {
		return nil, "", ErrNotUploading
	}

	var got int32
	if err := tx.Get(&got, `SELECT count(*) FROM dataset_chunks WHERE dataset_id = $1`, d.ID); err != nil {
		return nil, "", err
	}
	if got != d.ChunkCount {
		return nil, "", fmt.Errorf("%w: %d of %d present", ErrMissingChunks, got, d.ChunkCount)
	}

	var chunks []models.Chunk
	if err := tx.Select(&chunks,
		`SELECT id, dataset_id, chunk_index, offset_bytes, length, digest, object_id, uploaded_at
		 FROM dataset_chunks WHERE dataset_id = $1 ORDER BY chunk_index`, d.ID); err != nil {
		return nil, "", err
	}

	// Geometry validation: contiguous coverage of exactly total_size.
	chunkDigests := make([]string, 0, len(chunks))
	for i, c := range chunks {
		wantOff, wantLen, gerr := ExpectedChunk(d, int32(i))
		if gerr != nil || c.OffsetBytes != wantOff || c.Length != wantLen {
			return nil, "", fmt.Errorf("%w: chunk %d geometry", ErrMissingChunks, i)
		}
		chunkDigests = append(chunkDigests, c.Digest)
	}

	// Assemble into a staging file while holds chunk rows; compute digest.
	staging, wholeSize, wholeDigest, err := s.assemble(chunks)
	if err != nil {
		s.fs.RemoveStaging(staging)
		return nil, "", err
	}
	defer s.fs.RemoveStaging(staging)

	if wholeSize != d.TotalSize {
		return nil, "", fmt.Errorf("%w: assembled %d, declared %d", ErrSizeMismatch, wholeSize, d.TotalSize)
	}
	if d.WholeDigest.Valid && d.WholeDigest.String != wholeDigest {
		return nil, "", fmt.Errorf("%w: computed %s", ErrWholeMismatch, wholeDigest)
	}

	// Fault injection: simulate dying after assembly, before commit.
	if s.Faults.AfterAssemble != nil {
		if ferr := s.Faults.AfterAssemble(d.ID); ferr != nil {
			return nil, "", ferr
		}
	}

	wholeObjectID, err := s.obj.Acquire(tx, wholeDigest, wholeSize, staging)
	if err != nil {
		return nil, "", err
	}

	// Drop the dataset's references to individual chunks. The whole object
	// carries the bytes now. Chunk objects may be shared with other datasets;
	// only unreferenced bytes hit the graveyard. Rows must be deleted BEFORE
	// the object refs are released, or the FK from dataset_chunks blocks the
	// object-row delete.
	if _, err := tx.Exec(`DELETE FROM dataset_chunks WHERE dataset_id = $1`, d.ID); err != nil {
		return nil, "", err
	}
	graveyard, err := s.obj.ReleaseManyLocked(tx, chunkDigests)
	if err != nil {
		return nil, "", err
	}

	var updated models.Dataset
	if err := tx.Get(&updated,
		`UPDATE datasets
		 SET status = 'ready', whole_object_id = $2, published_at = now()
		 WHERE id = $1
		 RETURNING id, owner_id, name, total_size, chunk_size, chunk_count, whole_digest,
		           whole_object_id, status, created_at, published_at`,
		d.ID, wholeObjectID); err != nil {
		return nil, "", err
	}

	if err := tx.Commit(); err != nil {
		return nil, "", err
	}

	// Commit succeeded: unlink any quarantined blob files.
	for _, p := range graveyard {
		_ = removeFile(p)
	}
	if s.Faults.AfterPublishCommit != nil {
		s.Faults.AfterPublishCommit(d.ID)
	}
	return &updated, wholeDigest, nil
}

// assemble concatenates chunk objects in order into a new staging file and
// returns its path, size and SHA-256.
func (s *Service) assemble(chunks []models.Chunk) (string, int64, string, error) {
	out, err := s.fs.NewStagingFile()
	if err != nil {
		return "", 0, "", err
	}
	staging := out.Name()
	hasher := sha256.New()
	w := io.MultiWriter(out, hasher)

	var total int64
	for _, c := range chunks {
		f, oerr := s.fs.OpenObject(c.Digest)
		if oerr != nil {
			_ = out.Close()
			return staging, 0, "", fmt.Errorf("missing chunk object %s: %w", c.Digest, oerr)
		}
		n, cerr := io.CopyN(w, f, int64(c.Length))
		_ = f.Close()
		if cerr != nil {
			_ = out.Close()
			return staging, 0, "", fmt.Errorf("reading chunk %d: %w", c.ChunkIndex, cerr)
		}
		if n != int64(c.Length) {
			_ = out.Close()
			return staging, 0, "", fmt.Errorf("chunk %d truncated on disk: %d/%d", c.ChunkIndex, n, c.Length)
		}
		total += n
	}
	if err := out.Sync(); err != nil {
		_ = out.Close()
		return staging, 0, "", err
	}
	if err := out.Close(); err != nil {
		return staging, 0, "", err
	}
	return staging, total, hex.EncodeToString(hasher.Sum(nil)), nil
}

func (s *Service) getOwned(ctx context.Context, id, callerID int64) (*models.Dataset, error) {
	var d models.Dataset
	err := s.db.GetContext(ctx, &d,
		`SELECT id, owner_id, name, total_size, chunk_size, chunk_count, whole_digest,
		        whole_object_id, status, created_at, published_at
		 FROM datasets WHERE id = $1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if d.OwnerID != callerID {
		return nil, ErrForbidden
	}
	return &d, nil
}

// Get returns a dataset if the caller owns it.
func (s *Service) Get(ctx context.Context, callerID, id int64) (*models.Dataset, error) {
	return s.getOwned(ctx, id, callerID)
}

// ChunkStatus reports which slots are filled (for resumable clients).
func (s *Service) ChunkStatus(ctx context.Context, callerID, id int64) (map[int32]ChunkInfo, error) {
	d, err := s.getOwned(ctx, id, callerID)
	if err != nil {
		return nil, err
	}
	rows, err := s.db.QueryxContext(ctx,
		`SELECT chunk_index, length, digest FROM dataset_chunks WHERE dataset_id = $1 ORDER BY chunk_index`, d.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int32]ChunkInfo{}
	for rows.Next() {
		var idx int32
		var length int32
		var digest string
		if err := rows.Scan(&idx, &length, &digest); err != nil {
			return nil, err
		}
		out[idx] = ChunkInfo{Offset: int64(idx) * int64(d.ChunkSize), Length: length, Digest: digest}
	}
	return out, rows.Err()
}

type ChunkInfo struct {
	Offset int64  `json:"offset"`
	Length int32  `json:"length"`
	Digest string `json:"digest"`
}

// List returns the caller's datasets, newest first.
func (s *Service) List(ctx context.Context, callerID int64, limit, offset int) ([]models.Dataset, error) {
	var out []models.Dataset
	err := s.db.SelectContext(ctx, &out,
		`SELECT id, owner_id, name, total_size, chunk_size, chunk_count, whole_digest,
		        whole_object_id, status, created_at, published_at
		 FROM datasets WHERE owner_id = $1 ORDER BY id DESC LIMIT $2 OFFSET $3`,
		callerID, limit, offset)
	return out, err
}

// Delete removes a dataset. It is refused when any released model version is
// bound to this dataset. The whole-object reference (ready datasets) and every
// chunk-object reference (uploading datasets) are dropped inside one
// transaction; a shared file survives until its last reference goes.
func (s *Service) Delete(ctx context.Context, callerID, id int64) error {
	d, err := s.getOwned(ctx, id, callerID)
	if err != nil {
		return err
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	if _, err := tx.Exec(`SELECT id FROM datasets WHERE id = $1 FOR UPDATE`, d.ID); err != nil {
		return err
	}

	var refs int
	if err := tx.Get(&refs, `SELECT count(*) FROM model_versions WHERE dataset_id = $1`, d.ID); err != nil {
		return err
	}
	if refs > 0 {
		return ErrInUse
	}

	var digests []string
	if d.WholeObjectID.Valid {
		if err := tx.Select(&digests, `SELECT digest FROM objects WHERE id = $1`, d.WholeObjectID.Int64); err != nil {
			return err
		}
	}
	var chunkDigests []string
	if err := tx.Select(&chunkDigests, `SELECT digest FROM dataset_chunks WHERE dataset_id = $1`, d.ID); err != nil {
		return err
	}
	digests = append(digests, chunkDigests...)

	// Remove the dataset row (cascading dataset_chunks) BEFORE releasing the
	// object refs: the datasets.whole_object_id foreign key would otherwise
	// block the object-row delete.
	if _, err := tx.Exec(`DELETE FROM datasets WHERE id = $1`, d.ID); err != nil {
		return err
	}
	graveyard, err := s.obj.ReleaseManyLocked(tx, digests)
	if err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	for _, p := range graveyard {
		_ = removeFile(p)
	}
	return nil
}

// OpenContent opens the assembled blob of a ready dataset.
func (s *Service) OpenContent(ctx context.Context, callerID, id int64) (*models.Dataset, string, io.ReadCloser, error) {
	d, err := s.getOwned(ctx, id, callerID)
	if err != nil {
		return nil, "", nil, err
	}
	if d.Status != models.StatusReady || !d.WholeObjectID.Valid {
		return nil, "", nil, ErrNotUploading
	}
	var digest string
	if err := s.db.GetContext(ctx, &digest, `SELECT digest FROM objects WHERE id = $1`, d.WholeObjectID.Int64); err != nil {
		return nil, "", nil, err
	}
	f, err := s.fs.OpenObject(digest)
	if err != nil {
		return nil, "", nil, err
	}
	return d, digest, f, nil
}

// ReadyForBinding locks and returns a ready dataset's whole digest and object
// id for a caller who must own it. Used during model registration.
func (s *Service) ReadyForBinding(ctx context.Context, callerID, id int64) (digest string, objectID int64, err error) {
	d, gerr := s.getOwned(ctx, id, callerID)
	if gerr != nil {
		return "", 0, gerr
	}
	if d.Status != models.StatusReady || !d.WholeObjectID.Valid {
		return "", 0, ErrNotUploading
	}
	if qerr := s.db.GetContext(ctx, &digest,
		`SELECT o.digest FROM objects o JOIN datasets d ON d.whole_object_id = o.id
		 WHERE d.id = $1`, d.ID); qerr != nil {
		return "", 0, qerr
	}
	return digest, d.WholeObjectID.Int64, nil
}

func removeFile(p string) error {
	return os.Remove(p)
}
