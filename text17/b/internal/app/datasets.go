package app

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"time"

	"github.com/jmoiron/sqlx"
)

// Dataset lifecycle statuses.
const (
	DatasetStatusUploading = "uploading"
	DatasetStatusMerging   = "merging"
	DatasetStatusPublished = "published"
)

// DatasetService implements chunked upload, merge/publish and deletion.
type DatasetService struct {
	DB *sqlx.DB
	FS *FileStore
}

// CreateDatasetInput is the POST /datasets request body.
type CreateDatasetInput struct {
	Name      string `json:"name"`
	TotalSize int64  `json:"total_size"`
	ChunkSize int64  `json:"chunk_size"`
	SHA256    string `json:"sha256"`
}

func validSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// Create registers a dataset declaration: total size, fixed chunk size and
// the SHA-256 of the full content the client promises to deliver.
func (s *DatasetService) Create(ctx context.Context, owner string, in CreateDatasetInput) (*Dataset, error) {
	if in.Name == "" {
		return nil, BadRequestf("name is required")
	}
	if in.TotalSize <= 0 {
		return nil, BadRequestf("total_size must be positive")
	}
	if in.ChunkSize <= 0 {
		return nil, BadRequestf("chunk_size must be positive")
	}
	if !validSHA256(in.SHA256) {
		return nil, BadRequestf("sha256 must be a 64-character hex digest of the full content")
	}
	chunkCount := int((in.TotalSize + in.ChunkSize - 1) / in.ChunkSize)

	var ds Dataset
	err := s.DB.GetContext(ctx, &ds, `
		INSERT INTO datasets (owner_id, name, total_size, chunk_size, chunk_count, sha256)
		VALUES ($1,$2,$3,$4,$5,$6)
		RETURNING *`,
		owner, in.Name, in.TotalSize, in.ChunkSize, chunkCount, in.SHA256)
	if err != nil {
		return nil, err
	}
	return &ds, nil
}

// getDatasetForUpdate locks the dataset row for the duration of a
// transaction and enforces ownership. Another owner always sees 404.
func getDatasetForUpdate(ctx context.Context, tx *sqlx.Tx, id int64, owner string) (*Dataset, error) {
	var ds Dataset
	err := tx.GetContext(ctx, &ds, `SELECT * FROM datasets WHERE id=$1 FOR UPDATE`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("dataset %d not found", id)
	}
	if err != nil {
		return nil, err
	}
	if ds.OwnerID != owner {
		return nil, NotFoundf("dataset %d not found", id)
	}
	return &ds, nil
}

// Get returns one dataset with its upload progress for resume.
func (s *DatasetService) Get(ctx context.Context, owner string, id int64) (*DatasetView, error) {
	var ds Dataset
	err := s.DB.GetContext(ctx, &ds, `SELECT * FROM datasets WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("dataset %d not found", id)
	}
	if err != nil {
		return nil, err
	}
	if ds.OwnerID != owner {
		return nil, NotFoundf("dataset %d not found", id)
	}

	uploaded := []int{}
	if err := s.DB.SelectContext(ctx, &uploaded,
		`SELECT chunk_index FROM dataset_chunks WHERE dataset_id=$1 ORDER BY chunk_index`, id); err != nil {
		return nil, err
	}
	view := &DatasetView{Dataset: ds, UploadedChunks: uploaded, MissingChunks: []int{}}
	if ds.Status != DatasetStatusPublished {
		have := make(map[int]bool, len(uploaded))
		for _, i := range uploaded {
			have[i] = true
		}
		for i := 0; i < ds.ChunkCount; i++ {
			if !have[i] {
				view.MissingChunks = append(view.MissingChunks, i)
			}
		}
	}
	return view, nil
}

// List returns the caller's datasets, oldest first.
func (s *DatasetService) List(ctx context.Context, owner string) ([]Dataset, error) {
	datasets := []Dataset{}
	err := s.DB.SelectContext(ctx, &datasets,
		`SELECT * FROM datasets WHERE owner_id=$1 ORDER BY id`, owner)
	return datasets, err
}

// UploadChunk stores one chunk.
//
//   - The body must hash to the X-Chunk-SHA256 header (400 otherwise).
//   - Re-uploading the same index with identical content is an idempotent
//     no-op returning 200.
//   - The same index with different content is a 409 conflict.
//
// The chunk row is the durable "already received" marker; chunks may arrive
// in any order and a later publish just requires the complete, contiguous set.
func (s *DatasetService) UploadChunk(ctx context.Context, owner string, datasetID int64,
	index int, body []byte, declaredSHA string) (chunk *DatasetChunk, created bool, err error) {
	if !validSHA256(declaredSHA) {
		return nil, false, BadRequestf("X-Chunk-SHA256 header must be a 64-character hex digest")
	}
	sum := sha256.Sum256(body)
	digest := hex.EncodeToString(sum[:])
	if digest != declaredSHA {
		return nil, false, BadRequestf("chunk content does not match X-Chunk-SHA256 header")
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback()

	ds, err := getDatasetForUpdate(ctx, tx, datasetID, owner)
	if err != nil {
		return nil, false, err
	}
	if ds.Status != DatasetStatusUploading {
		return nil, false, Conflictf("dataset %d is not accepting chunks (status=%s)", datasetID, ds.Status)
	}
	if index < 0 || index >= ds.ChunkCount {
		return nil, false, BadRequestf("chunk index %d out of range [0,%d)", index, ds.ChunkCount)
	}
	// Every chunk is chunk_size bytes except the final one, which carries the
	// remainder of total_size.
	expected := ds.ChunkSize
	if index == ds.ChunkCount-1 {
		expected = ds.TotalSize - ds.ChunkSize*int64(ds.ChunkCount-1)
	}
	if int64(len(body)) != expected {
		return nil, false, BadRequestf("chunk %d must be exactly %d bytes, got %d", index, expected, len(body))
	}

	var existing DatasetChunk
	err = tx.GetContext(ctx, &existing,
		`SELECT * FROM dataset_chunks WHERE dataset_id=$1 AND chunk_index=$2`, datasetID, index)
	switch {
	case err == nil:
		// Idempotent retransmit or content conflict.
		if existing.SHA256 != digest {
			return nil, false, Conflictf("chunk %d already uploaded with different content", index)
		}
		if err := tx.Commit(); err != nil {
			return nil, false, err
		}
		return &existing, false, nil
	case !errors.Is(err, sql.ErrNoRows):
		return nil, false, err
	}

	if err := s.FS.WriteChunk(datasetID, index, body); err != nil {
		return nil, false, err
	}
	var newChunk DatasetChunk
	if err := tx.GetContext(ctx, &newChunk, `
		INSERT INTO dataset_chunks (dataset_id, chunk_index, sha256, size)
		VALUES ($1,$2,$3,$4) RETURNING *`,
		datasetID, index, digest, len(body)); err != nil {
		return nil, false, err
	}
	if err := tx.Commit(); err != nil {
		return nil, false, err
	}
	return &newChunk, true, nil
}

// Publish merges all chunks into one content-addressed file and marks the
// dataset published, all inside one transaction.
//
//   - Missing chunks or a non-contiguous index set fail with 409 and leave
//     the dataset uploading.
//   - A merged digest that differs from the declared overall SHA-256 fails
//     with 409 and leaves the dataset uploading; the tmp file is removed.
//   - Concurrent publishes serialize on the dataset row lock; waiters see
//     the committed published state and return success idempotently.
//   - A crash before commit rolls the database back (status stays uploading);
//     the tmp merge file is reaped at next startup. A half-merged file is
//     therefore never marked usable.
func (s *DatasetService) Publish(ctx context.Context, owner string, id int64) (*Dataset, error) {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	ds, err := getDatasetForUpdate(ctx, tx, id, owner)
	if err != nil {
		return nil, err
	}
	if ds.Status == DatasetStatusPublished {
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return ds, nil
	}
	if ds.Status != DatasetStatusUploading {
		return nil, Conflictf("dataset %d is not publishable (status=%s)", id, ds.Status)
	}

	chunks := []DatasetChunk{}
	if err := tx.SelectContext(ctx, &chunks,
		`SELECT * FROM dataset_chunks WHERE dataset_id=$1 ORDER BY chunk_index`, id); err != nil {
		return nil, err
	}
	if len(chunks) != ds.ChunkCount {
		return nil, Conflictf("dataset %d has %d of %d chunks uploaded", id, len(chunks), ds.ChunkCount)
	}
	for i, c := range chunks {
		if c.ChunkIndex != i {
			return nil, Conflictf("dataset %d is missing chunk %d", id, i)
		}
	}

	// Merge into the private tmp file while holding the dataset row lock,
	// re-verifying every chunk's digest and computing the overall digest.
	// The merge touches only tmp; promotion and the state flip happen in the
	// same transaction below. A crash rolls the transaction back (status stays
	// uploading) and the leftover tmp file is reaped at next startup, so a
	// half-merged file can never be marked usable.
	tmpPath := s.FS.MergeTmpPath(id)
	digest, err := s.FS.MergeChunks(id, chunks)
	if err != nil {
		return nil, err
	}
	if digest != ds.SHA256 {
		_ = s.FS.RemoveTmp(tmpPath)
		return nil, Conflictf("merged content digest %s does not match declared sha256 %s", digest, ds.SHA256)
	}

	fileID, err := addFileRef(ctx, s.FS, tx, digest, ds.TotalSize, tmpPath)
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	if _, err := tx.ExecContext(ctx, `
		UPDATE datasets SET status=$1, file_id=$2, published_at=$3 WHERE id=$4`,
		DatasetStatusPublished, fileID, now, id); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	ds.Status = DatasetStatusPublished
	ds.FileID = &fileID
	ds.PublishedAt = &now
	return ds, nil
}

// Delete removes a dataset if nothing references it, then drops its chunks
// and releases its share of the content file.
func (s *DatasetService) Delete(ctx context.Context, owner string, id int64) error {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	ds, err := getDatasetForUpdate(ctx, tx, id, owner)
	if err != nil {
		return err
	}

	var refs int
	if err := tx.GetContext(ctx, &refs, `
		SELECT (SELECT count(*) FROM model_versions WHERE dataset_id=$1)
		     + (SELECT count(*) FROM experiments   WHERE dataset_id=$1)`, id); err != nil {
		return err
	}
	if refs > 0 {
		return Conflictf("dataset %d is still referenced by %d model version(s)/experiment(s)", id, refs)
	}

	if _, err := tx.ExecContext(ctx, `DELETE FROM datasets WHERE id=$1`, id); err != nil {
		return err
	}
	gcSHA := ""
	if ds.FileID != nil {
		if gcSHA, err = releaseFileRef(ctx, tx, *ds.FileID); err != nil {
			return err
		}
	}
	if err := tx.Commit(); err != nil {
		return err
	}

	// Disk cleanup after commit. Chunk dir removal is idempotent; the content
	// file unlink is guarded by the advisory lock inside GCOrphanFile.
	if err := s.FS.RemoveChunkDir(id); err != nil {
		return err
	}
	if gcSHA != "" {
		if err := GCOrphanFile(ctx, s.DB, s.FS, gcSHA); err != nil {
			return err
		}
	}
	return nil
}

// Recover reconciles the database and the data directory after a crash. It
// runs once at startup before traffic is accepted:
//
//  1. Any dataset left in 'merging' is reset to 'uploading' (its publish did
//     not commit, so it is not usable);
//  2. tmp merge files are removed;
//  3. published files with no files row (crash between rename and commit) are
//     swept;
//  4. chunk directories without a dataset row are swept.
func (s *DatasetService) Recover(ctx context.Context) error {
	if _, err := s.DB.ExecContext(ctx,
		`UPDATE datasets SET status=$1 WHERE status=$2`,
		DatasetStatusUploading, DatasetStatusMerging); err != nil {
		return err
	}
	if err := s.FS.CleanupTmp(); err != nil {
		return err
	}

	var shas []string
	if err := s.DB.SelectContext(ctx, &shas, `SELECT sha256 FROM files`); err != nil {
		return err
	}
	keep := make(map[string]bool, len(shas))
	for _, sha := range shas {
		keep[sha] = true
	}
	if err := s.FS.SweepFiles(keep); err != nil {
		return err
	}

	var ids []int64
	if err := s.DB.SelectContext(ctx, &ids, `SELECT id FROM datasets`); err != nil {
		return err
	}
	keepIDs := make(map[int64]bool, len(ids))
	for _, id := range ids {
		keepIDs[id] = true
	}
	return s.FS.SweepChunkDirs(keepIDs)
}
