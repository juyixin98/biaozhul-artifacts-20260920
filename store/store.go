// Package store implements the local content-addressed artifact store.
//
// On-disk layout (root is separate from any build working directory):
//
//	root/
//	  blobs/sha256/<hex[0:2]>/<hex>   # immutable, read-only published objects
//	  tmp/                             # in-flight uploads (staged, never visible)
//	  quarantine/                      # objects that failed digest verification
//
// Publication is atomic: the uploaded bytes are written to a private file in
// tmp/, hashed, and renamed into blobs/ only when the digest matches. A crash
// or interrupted upload leaves at most an orphan in tmp/, which Sweep removes
// on startup. Partial objects are therefore never reachable through the
// blobs/ namespace.
package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"modelcache/digest"
)

// DefaultMaxObjectSize is the per-object size cap (100 MiB) used when
// Options.MaxObjectSize is zero.
const DefaultMaxObjectSize int64 = 100 << 20

// Sentinel errors. Callers can diagnose failure modes via errors.Is/As.
var (
	// ErrNotFound means no published object exists for the digest.
	ErrNotFound = errors.New("object not found")
	// ErrTooLarge means the incoming object exceeds the configured size cap.
	ErrTooLarge = errors.New("object exceeds maximum allowed size")
	// ErrDigestMismatch means the uploaded bytes hash to something other than
	// the claimed digest. The bad bytes are quarantined rather than published.
	ErrDigestMismatch = errors.New("digest mismatch")
	// ErrSizeMismatch means a declared Content-Length did not match the bytes
	// actually received.
	ErrSizeMismatch = errors.New("declared size does not match received bytes")
	// ErrCorrupt means a published object no longer matches its digest
	// (bit rot, tampering, or a bad cache directory copied in by an operator).
	ErrCorrupt = errors.New("stored object is corrupt")
)

// CorruptionError decorates ErrCorrupt with the digest that failed.
type CorruptionError struct {
	Digest digest.Digest
}

func (e *CorruptionError) Error() string {
	return fmt.Sprintf("%v: %s", ErrCorrupt, e.Digest)
}

func (e *CorruptionError) Unwrap() error { return ErrCorrupt }

// Options configures a Store.
type Options struct {
	// Root is the cache root directory. It is distinct from build working dirs.
	Root string
	// MaxObjectSize caps a single object; 0 means DefaultMaxObjectSize.
	MaxObjectSize int64
	// Now supplies the current time (overridable in tests).
	Now func() time.Time
}

// Store is a local content-addressed object store.
type Store struct {
	root          string
	blobsDir      string
	tmpDir        string
	quarantineDir string
	maxObjectSize int64
	now           func() time.Time

	// Singleflight for in-flight uploads keyed by digest hex: concurrent Puts
	// of the same object elect one leader to stage the bytes; followers wait
	// and then take the fast path once the blob is published.
	flightMu sync.Mutex
	flights  map[string]*uploadFlight
}

// uploadFlight coordinates waiters for one digest.
type uploadFlight struct {
	done chan struct{}
	err  error
}

// New creates the on-disk directory structure and returns a Store.
func New(opts Options) (*Store, error) {
	if opts.Root == "" {
		return nil, errors.New("store: root directory is required")
	}
	maxSize := opts.MaxObjectSize
	if maxSize <= 0 {
		maxSize = DefaultMaxObjectSize
	}
	now := opts.Now
	if now == nil {
		now = time.Now
	}
	s := &Store{
		root:          opts.Root,
		blobsDir:      filepath.Join(opts.Root, "blobs"),
		tmpDir:        filepath.Join(opts.Root, "tmp"),
		quarantineDir: filepath.Join(opts.Root, "quarantine"),
		maxObjectSize: maxSize,
		now:           now,
		flights:       map[string]*uploadFlight{},
	}
	for _, d := range []string{s.blobsDir, s.tmpDir, s.quarantineDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("store: create %s: %w", d, err)
		}
	}
	return s, nil
}

// MaxObjectSize returns the configured per-object size cap.
func (s *Store) MaxObjectSize() int64 { return s.maxObjectSize }

// Root returns the cache root directory.
func (s *Store) Root() string { return s.root }

// limitedReader enforces a hard byte cap. It allows underlying reads one byte
// beyond the remaining budget so that a stream ending exactly at the cap
// surfaces a normal EOF, while a (max+1)th real byte returns ErrTooLarge.
type limitedReader struct {
	r      io.Reader
	remain int64
}

func (l *limitedReader) Read(p []byte) (int, error) {
	if int64(len(p)) > l.remain+1 {
		p = p[:l.remain+1]
	}
	n, err := l.r.Read(p)
	if int64(n) > l.remain {
		l.remain = 0
		return n, ErrTooLarge
	}
	l.remain -= int64(n)
	return n, err
}

// digestPath returns the canonical path of a published object and guarantees
// it stays inside blobsDir (defense against any weird digest encoding).
func (s *Store) digestPath(d digest.Digest) (string, error) {
	hexv := d.Hex()
	// Digest.Parse already validated the hex; re-check defensively.
	if len(hexv) != sha256.Size*2 {
		return "", fmt.Errorf("store: invalid digest %q", d)
	}
	dir := filepath.Join(s.blobsDir, d.Algorithm(), hexv[:2])
	return filepath.Join(dir, hexv), nil
}

// Put stages r as a temporary object and atomically publishes it under dgst.
//
// declaredSize is checked when non-negative. Concurrent Puts of the same
// digest are deduplicated with a singleflight: one caller stages the bytes,
// the others wait and observe the published blob (existed=true) when it lands.
// If the leader fails, waiters retry and may elect a new leader themselves.
func (s *Store) Put(ctx context.Context, dgst digest.Digest, declaredSize int64, r io.Reader) (int64, bool, error) {
	if dgst.Algorithm() != digest.AlgorithmSHA256 {
		return 0, false, fmt.Errorf("store: unsupported digest algorithm %q", dgst.Algorithm())
	}
	target, err := s.digestPath(dgst)
	if err != nil {
		return 0, false, err
	}

	const maxJoinAttempts = 64
	for attempt := 0; attempt < maxJoinAttempts; attempt++ {
		// Fast path: already present. Verify before claiming a hit so a
		// corrupt cache entry is diagnosed instead of silently served.
		if fi, statErr := os.Stat(target); statErr == nil {
			if declaredSize >= 0 && fi.Size() != declaredSize {
				return 0, false, fmt.Errorf("%w: stored size %d, declared %d",
					ErrSizeMismatch, fi.Size(), declaredSize)
			}
			if err := s.verifyAt(target, dgst); err != nil {
				return 0, false, err
			}
			return fi.Size(), true, nil
		} else if !errors.Is(statErr, os.ErrNotExist) {
			return 0, false, statErr
		}

		flight, leader := s.joinFlight(dgst.Hex())
		if !leader {
			select {
			case <-flight.done:
				// Re-check: the blob is now published, or the leader failed
				// and we may become the next leader.
				continue
			case <-ctx.Done():
				return 0, false, ctx.Err()
			}
		}

		size, upErr := s.stageAndPublish(dgst, declaredSize, r, target)
		s.finishFlight(dgst.Hex(), flight, upErr)
		if upErr != nil {
			return 0, false, upErr
		}
		return size, false, nil
	}
	return 0, false, errors.New("store: too many concurrent upload attempts")
}

// joinFlight returns the in-flight record for key. The first caller is the
// leader (leader=true) and owns staging the upload; all others wait.
func (s *Store) joinFlight(key string) (*uploadFlight, bool) {
	s.flightMu.Lock()
	defer s.flightMu.Unlock()
	if f, ok := s.flights[key]; ok {
		return f, false
	}
	f := &uploadFlight{done: make(chan struct{})}
	s.flights[key] = f
	return f, true
}

// finishFlight releases waiters and removes the in-flight record.
func (s *Store) finishFlight(key string, f *uploadFlight, err error) {
	s.flightMu.Lock()
	if s.flights[key] == f {
		delete(s.flights, key)
	}
	s.flightMu.Unlock()
	f.err = err
	close(f.done)
}

// stageAndPublish writes the incoming bytes to a private temp file, verifies
// their digest, and atomically renames the file into the blobs namespace.
func (s *Store) stageAndPublish(dgst digest.Digest, declaredSize int64, r io.Reader, target string) (int64, error) {
	tmp, err := s.newTempFile()
	if err != nil {
		return 0, err
	}
	tmpName := tmp.Name()
	committed := false
	defer func() {
		if !committed {
			tmp.Close()
			os.Remove(tmpName)
		}
	}()

	h := sha256.New()
	lr := &limitedReader{r: r, remain: s.maxObjectSize}
	n, copyErr := io.Copy(io.MultiWriter(tmp, h), lr)

	if declaredSize >= 0 && copyErr == nil && n != declaredSize {
		copyErr = fmt.Errorf("%w: declared %d, received %d", ErrSizeMismatch, declaredSize, n)
	}
	if copyErr != nil {
		return 0, copyErr
	}

	got := h.Sum(nil)
	if hex.EncodeToString(got) != dgst.Hex() {
		if qerr := s.quarantine(tmp, dgst, h, n); qerr != nil {
			return 0, fmt.Errorf("%w (quarantine failed: %v)",
				fmt.Errorf("%w: claimed %s, got %s", ErrDigestMismatch, dgst.Hex(), hex.EncodeToString(got)), qerr)
		}
		return 0, fmt.Errorf("%w: claimed %s, got %s",
			ErrDigestMismatch, dgst.Hex(), hex.EncodeToString(got))
	}

	if err := tmp.Chmod(0o444); err != nil {
		return 0, fmt.Errorf("store: make read-only: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return 0, fmt.Errorf("store: fsync temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return 0, fmt.Errorf("store: close temp: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return 0, fmt.Errorf("store: create blob dir: %w", err)
	}
	if err := os.Rename(tmpName, target); err != nil {
		return 0, fmt.Errorf("store: publish (rename): %w", err)
	}
	committed = true
	// fsync the parent directory so the rename survives power loss.
	syncDir(filepath.Dir(target))

	fi, err := os.Stat(target)
	if err != nil {
		return 0, err
	}
	return fi.Size(), nil
}

// newTempFile creates a private, 0600 staging file under tmp/.
func (s *Store) newTempFile() (*os.File, error) {
	f, err := os.CreateTemp(s.tmpDir, "upload-*.part")
	if err != nil {
		return nil, fmt.Errorf("store: create temp: %w", err)
	}
	if err := f.Chmod(0o600); err != nil {
		f.Close()
		os.Remove(f.Name())
		return nil, fmt.Errorf("store: chmod temp: %w", err)
	}
	return f, nil
}

// quarantine persists a digest-mismatching upload for later diagnosis.
func (s *Store) quarantine(f *os.File, claimed digest.Digest, h hash.Hash, n int64) error {
	got := hex.EncodeToString(h.Sum(nil))
	stamp := s.now().UTC().Format("20060102T150405.000000000")
	name := fmt.Sprintf("bad-claimed-%s-got-%s-%s.part", claimed.Hex()[:16], got[:16], stamp)
	dst := filepath.Join(s.quarantineDir, name)
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), dst); err != nil {
		return err
	}
	return nil
}

// verifyAt hashes the file at path and compares it to dgst.
func (s *Store) verifyAt(path string, dgst digest.Digest) error {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("%w: %s", ErrNotFound, dgst)
		}
		return err
	}
	defer f.Close()
	d, _, err := digest.FromReader(f)
	if err != nil {
		return err
	}
	if d.Hex() != dgst.Hex() {
		return &CorruptionError{Digest: dgst}
	}
	return nil
}

// Get opens a published object for reading. Callers must close the returned
// file. Missing objects return ErrNotFound.
func (s *Store) Get(ctx context.Context, dgst digest.Digest) (*os.File, os.FileInfo, error) {
	path, err := s.digestPath(dgst)
	if err != nil {
		return nil, nil, err
	}
	f, fi, err := openFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil, fmt.Errorf("%w: %s", ErrNotFound, dgst)
		}
		return nil, nil, err
	}
	return f, fi, nil
}

// Verify re-hashes a published object. On failure the object is moved to
// quarantine (so the cache self-heals on the next upload) and ErrCorrupt is
// returned.
func (s *Store) Verify(ctx context.Context, dgst digest.Digest) (int64, error) {
	path, err := s.digestPath(dgst)
	if err != nil {
		return 0, err
	}
	fi, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return 0, fmt.Errorf("%w: %s", ErrNotFound, dgst)
		}
		return 0, err
	}
	if err := s.verifyAt(path, dgst); err != nil {
		if cerr := s.quarantinePublished(path, dgst); cerr != nil {
			return fi.Size(), fmt.Errorf("%w (quarantine failed: %v)", err, cerr)
		}
		return fi.Size(), err
	}
	return fi.Size(), nil
}

// quarantinePublished moves an already-published corrupt blob aside.
func (s *Store) quarantinePublished(path string, dgst digest.Digest) error {
	stamp := s.now().UTC().Format("20060102T150405.000000000")
	dst := filepath.Join(s.quarantineDir, fmt.Sprintf("corrupt-%s-%s.part", dgst.Hex()[:16], stamp))
	return os.Rename(path, dst)
}

// Delete removes a published object (cache eviction). Missing objects are fine.
func (s *Store) Delete(ctx context.Context, dgst digest.Digest) error {
	path, err := s.digestPath(dgst)
	if err != nil {
		return err
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// BlobInfo describes a published object.
type BlobInfo struct {
	Digest digest.Digest `json:"digest"`
	Size   int64         `json:"size"`
}

// List returns all published objects sorted by digest.
func (s *Store) List(ctx context.Context) ([]BlobInfo, error) {
	algoDir := filepath.Join(s.blobsDir, digest.AlgorithmSHA256)
	entries, err := os.ReadDir(algoDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var out []BlobInfo
	for _, shard := range entries {
		if !shard.IsDir() || len(shard.Name()) != 2 {
			continue
		}
		files, err := os.ReadDir(filepath.Join(algoDir, shard.Name()))
		if err != nil {
			return nil, err
		}
		for _, f := range files {
			if f.IsDir() {
				continue
			}
			dgst, perr := digest.Parse(digest.AlgorithmSHA256 + ":" + f.Name())
			if perr != nil {
				continue // ignore non-blob junk
			}
			info, ierr := f.Info()
			if ierr != nil {
				continue
			}
			out = append(out, BlobInfo{Digest: dgst, Size: info.Size()})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Digest.Hex() < out[j].Digest.Hex() })
	return out, nil
}

// Stats summarizes cache contents.
type Stats struct {
	Objects       int   `json:"objects"`
	TotalBytes    int64 `json:"totalBytes"`
	MaxObjectSize int64 `json:"maxObjectSize"`
	TempFiles     int   `json:"tempFiles"`
	Quarantined   int   `json:"quarantined"`
}

// Stats computes a cache summary.
func (s *Store) Stats(ctx context.Context) (Stats, error) {
	blobs, err := s.List(ctx)
	if err != nil {
		return Stats{}, err
	}
	st := Stats{MaxObjectSize: s.maxObjectSize, Objects: len(blobs)}
	for _, b := range blobs {
		st.TotalBytes += b.Size
	}
	st.TempFiles = countFiles(s.tmpDir)
	st.Quarantined = countFiles(s.quarantineDir)
	return st, nil
}

func countFiles(dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	n := 0
	for _, e := range entries {
		if !e.IsDir() {
			n++
		}
	}
	return n
}

// Sweep removes orphaned staging files left by crashed/interrupted uploads.
// Called at server startup. It never touches blobs/ or quarantine/.
func (s *Store) Sweep(ctx context.Context) (int, error) {
	entries, err := os.ReadDir(s.tmpDir)
	if err != nil {
		return 0, err
	}
	removed := 0
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if err := os.Remove(filepath.Join(s.tmpDir, e.Name())); err == nil {
			removed++
		}
	}
	return removed, nil
}

func openFile(path string) (*os.File, os.FileInfo, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	if fi.IsDir() {
		f.Close()
		return nil, nil, fmt.Errorf("store: %s is a directory", path)
	}
	return f, fi, nil
}

func syncDir(dir string) {
	d, err := os.Open(dir)
	if err != nil {
		return
	}
	defer d.Close()
	_ = d.Sync() // best effort; meaningful on Linux, harmless elsewhere
}
