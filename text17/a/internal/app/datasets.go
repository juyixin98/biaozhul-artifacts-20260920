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

const (
	DatasetStatusUploading = "uploading"
	DatasetStatusMerging   = "merging"
	DatasetStatusPublished = "published"
)

type DatasetService struct {
	DB *sqlx.DB
	FS *FileStore
}

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
		RETURNING *`, owner, in.Name, in.TotalSize, in.ChunkSize, chunkCount, in.SHA256)
	if err != nil {
		return nil, err
	}
	return &ds, nil
}

// getDatasetForUpdate locks the dataset row and enforces ownership. Other
// owners see 404, never 403, so resource existence is not leaked.
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

type DatasetView struct {
	Dataset
	UploadedChunks []int `json:"uploaded_chunks"`
	MissingChunks  []int `json:"missing_chunks"`
}

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

func (s *DatasetService) List(ctx context.Context, owner string) ([]Dataset, error) {
	datasets := []Dataset{}
	err := s.DB.SelectContext(ctx, &datasets,
		`SELECT * FROM datasets WHERE owner_id=$1 ORDER BY id`, owner)
	return datasets, err
}

// UploadChunk stores one chunk. Re-uploading identical content is a no-op
// (idempotent); different content for an already-uploaded index is rejected.
// The dataset row lock serializes concurrent uploads of the same dataset.
func (s *DatasetService) UploadChunk(ctx context.Context, owner string, datasetID int64, index int, body []byte, declaredSHA string) (*DatasetChunk, bool, error) {
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
	var chunk DatasetChunk
	if err := tx.GetContext(ctx, &chunk, `
		INSERT INTO dataset_chunks (dataset_id, chunk_index, sha256, size)
		VALUES ($1,$2,$3,$4) RETURNING *`, datasetID, index, digest, len(body)); err != nil {
		return nil, false, err
	}
	if err := tx.Commit(); err != nil {
		return nil, false, err
	}
	return &chunk, true, nil
}

// Publish merges all chunks, verifies the overall digest, and atomically
// marks the dataset published. Concurrent publishes serialize on the dataset
// row lock: the loser observes the committed 'published' state and returns
// success idempotently. A crash mid-publish rolls the transaction back, so a
// half-merged file is never marked usable; the tmp file is removed by
// recovery at next startup.
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
	if _, err := tx.ExecContext(ctx,
		`UPDATE datasets SET status=$1, file_id=$2, published_at=$3 WHERE id=$4`,
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

// Delete removes a dataset after verifying nothing references it. Shared
// content files are released only when the last referencing dataset is gone.
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
	_ = s.FS.RemoveChunkDir(id)
	if gcSHA != "" {
		// Serialized against concurrent publishers re-referencing the same
		// content by the per-digest advisory lock inside GCOrphanFile.
		if err := GCOrphanFile(ctx, s.DB, s.FS, gcSHA); err != nil {
			return err
		}
	}
	return nil
}

// Recover cleans up after a crash: reset interrupted publishes, drop
// half-written merge outputs, and remove on-disk files no database row
// points to. Must run before the server accepts traffic.
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
