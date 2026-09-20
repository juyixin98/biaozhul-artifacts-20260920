package app

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
)

// FileStore manages the on-disk layout:
//
//	<root>/chunks/<datasetID>/<index>   uploaded chunks (pre-publish)
//	<root>/tmp/merge-<datasetID>.part   in-progress merges (never served)
//	<root>/files/<sha256>               published, content-addressed files
//
// Invariant: a committed row in the files table implies the file exists on
// disk. Disk deletion only happens under the per-digest advisory lock after
// the row is gone (see GCOrphanFile), and crash orphans are swept at startup.
type FileStore struct {
	Root string
}

func NewFileStore(root string) (*FileStore, error) {
	fs := &FileStore{Root: root}
	for _, d := range []string{fs.FilesDir(), fs.TmpDir(), filepath.Join(root, "chunks")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	return fs, nil
}

func (f *FileStore) FilesDir() string { return filepath.Join(f.Root, "files") }
func (f *FileStore) TmpDir() string   { return filepath.Join(f.Root, "tmp") }

func (f *FileStore) FilePath(sha256Hex string) string {
	return filepath.Join(f.FilesDir(), sha256Hex)
}

func (f *FileStore) MergeTmpPath(datasetID int64) string {
	return filepath.Join(f.TmpDir(), "merge-"+strconv.FormatInt(datasetID, 10)+".part")
}

func (f *FileStore) ChunkDir(datasetID int64) string {
	return filepath.Join(f.Root, "chunks", strconv.FormatInt(datasetID, 10))
}

func (f *FileStore) ChunkPath(datasetID int64, index int) string {
	return filepath.Join(f.ChunkDir(datasetID), strconv.Itoa(index))
}

// WriteChunk atomically stores a chunk body (temp file + rename). Callers
// hold the dataset row lock, so concurrent writes to the same index cannot
// interleave.
func (f *FileStore) WriteChunk(datasetID int64, index int, body []byte) error {
	dir := f.ChunkDir(datasetID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".upload-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(body); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	return os.Rename(tmpName, f.ChunkPath(datasetID, index))
}

// MergeChunks concatenates all chunks into the tmp merge file, verifying each
// chunk against its recorded digest, and returns the overall SHA-256 hex
// digest. On any failure the tmp file is removed.
func (f *FileStore) MergeChunks(datasetID int64, chunks []DatasetChunk) (string, error) {
	tmpPath := f.MergeTmpPath(datasetID)
	out, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return "", err
	}
	fail := func(err error) (string, error) {
		out.Close()
		os.Remove(tmpPath)
		return "", err
	}
	h := sha256.New()
	for _, c := range chunks {
		err := func() error {
			in, err := os.Open(f.ChunkPath(datasetID, c.ChunkIndex))
			if err != nil {
				return Conflictf("chunk %d file missing on disk: %v", c.ChunkIndex, err)
			}
			defer in.Close()
			ch := sha256.New()
			n, err := io.Copy(io.MultiWriter(out, h, ch), in)
			if err != nil {
				return fmt.Errorf("merge chunk %d: %w", c.ChunkIndex, err)
			}
			if n != c.Size {
				return Conflictf("chunk %d size on disk (%d) does not match record (%d)", c.ChunkIndex, n, c.Size)
			}
			if got := hex.EncodeToString(ch.Sum(nil)); got != c.SHA256 {
				return Conflictf("chunk %d content on disk does not match recorded digest", c.ChunkIndex)
			}
			return nil
		}()
		if err != nil {
			return fail(err)
		}
	}
	if err := out.Sync(); err != nil {
		return fail(err)
	}
	if err := out.Close(); err != nil {
		return fail(err)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// PromoteMerged atomically moves the merged tmp file into the content store.
// If the content already exists on disk (same digest => same content), the
// tmp copy is simply discarded.
func (f *FileStore) PromoteMerged(tmpPath, sha256Hex string) error {
	final := f.FilePath(sha256Hex)
	if _, err := os.Stat(final); err == nil {
		return os.Remove(tmpPath)
	}
	return os.Rename(tmpPath, final)
}

func (f *FileStore) RemoveChunkDir(datasetID int64) error {
	return os.RemoveAll(f.ChunkDir(datasetID))
}

// RemoveTmp discards an in-progress merge file (e.g. after a digest mismatch).
func (f *FileStore) RemoveTmp(tmpPath string) error {
	if err := os.Remove(tmpPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// CleanupTmp removes in-progress merge files left behind by a crash.
func (f *FileStore) CleanupTmp() error {
	entries, err := os.ReadDir(f.TmpDir())
	if err != nil {
		return err
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(f.TmpDir(), e.Name())); err != nil {
			return err
		}
	}
	return nil
}

// SweepFiles deletes published files that have no files-table row (crash
// orphans). Only safe to call before the server accepts traffic.
func (f *FileStore) SweepFiles(keep map[string]bool) error {
	entries, err := os.ReadDir(f.FilesDir())
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.IsDir() || keep[e.Name()] {
			continue
		}
		if err := os.Remove(filepath.Join(f.FilesDir(), e.Name())); err != nil {
			return err
		}
	}
	return nil
}

// SweepChunkDirs deletes chunk directories of datasets that no longer exist.
func (f *FileStore) SweepChunkDirs(keep map[int64]bool) error {
	base := filepath.Join(f.Root, "chunks")
	entries, err := os.ReadDir(base)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id, err := strconv.ParseInt(e.Name(), 10, 64)
		if err != nil || !keep[id] {
			if err := os.RemoveAll(filepath.Join(base, e.Name())); err != nil {
				return err
			}
		}
	}
	return nil
}
