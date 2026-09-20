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
//	<root>/chunks/<datasetID>/<index>   received chunks (pre-publish)
//	<root>/tmp/merge-<datasetID>.part   in-progress merge output (never served)
//	<root>/files/<sha256>               published, content-addressed files
//
// Invariant: a committed files row implies the file exists on disk, and an
// on-disk file under files/ has a files row after recovery has run. Disk
// deletion of a published file only happens under the per-digest advisory
// lock, after the row is gone (see files.go).
type FileStore struct {
	Root string
}

// NewFileStore creates a store rooted at root and ensures its directories.
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

// WriteChunk stores a chunk body atomically (temp file, fsync, rename).
// Callers hold the dataset row lock, so two writers cannot target the same
// chunk path at once.
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
	abort := func() {
		tmp.Close()
		_ = os.Remove(tmpName)
	}
	if _, err := tmp.Write(body); err != nil {
		abort()
		return err
	}
	if err := tmp.Sync(); err != nil {
		abort()
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		return err
	}
	return os.Rename(tmpName, f.ChunkPath(datasetID, index))
}

// MergeChunks concatenates the chunks in order into a private tmp file,
// re-verifying every chunk's on-disk size and digest while streaming, and
// returns the SHA-256 of the concatenation. Any failure removes the tmp file
// so a partial merge is never left behind for another attempt to trip over.
func (f *FileStore) MergeChunks(datasetID int64, chunks []DatasetChunk) (string, error) {
	tmpPath := f.MergeTmpPath(datasetID)
	out, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return "", err
	}
	fail := func(err error) (string, error) {
		_ = out.Close()
		_ = os.Remove(tmpPath)
		return "", err
	}

	overall := sha256.New()
	for _, c := range chunks {
		err := func() error {
			in, err := os.Open(f.ChunkPath(datasetID, c.ChunkIndex))
			if err != nil {
				return Conflictf("chunk %d file missing on disk: %v", c.ChunkIndex, err)
			}
			defer in.Close()
			chunkHash := sha256.New()
			n, err := io.Copy(io.MultiWriter(out, overall, chunkHash), in)
			if err != nil {
				return fmt.Errorf("merge chunk %d: %w", c.ChunkIndex, err)
			}
			if n != c.Size {
				return Conflictf("chunk %d size on disk (%d) does not match record (%d)",
					c.ChunkIndex, n, c.Size)
			}
			if got := hex.EncodeToString(chunkHash.Sum(nil)); got != c.SHA256 {
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
	return hex.EncodeToString(overall.Sum(nil)), nil
}

// PromoteMerged moves the tmp merge output into the content store. The same
// digest means identical content, so if the target already exists the tmp
// copy is simply discarded; either outcome leaves exactly one good file.
func (f *FileStore) PromoteMerged(tmpPath, sha256Hex string) error {
	final := f.FilePath(sha256Hex)
	if _, err := os.Stat(final); err == nil {
		return os.Remove(tmpPath)
	} else if !os.IsNotExist(err) {
		return err
	}
	return os.Rename(tmpPath, final)
}

// RemoveChunkDir removes a dataset's chunk directory after deletion.
func (f *FileStore) RemoveChunkDir(datasetID int64) error {
	if err := os.RemoveAll(f.ChunkDir(datasetID)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// RemoveTmp removes an in-progress merge file (e.g. after a digest mismatch).
func (f *FileStore) RemoveTmp(tmpPath string) error {
	if err := os.Remove(tmpPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// CleanupTmp empties the tmp directory during startup recovery.
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

// SweepFiles deletes published files with no files-table row (crash orphans).
// Only safe before the server accepts traffic.
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

// SweepChunkDirs removes chunk directories belonging to datasets no longer
// present in the database.
func (f *FileStore) SweepChunkDirs(keep map[int64]bool) error {
	base := filepath.Join(f.Root, "chunks")
	entries, err := os.ReadDir(base)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
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
