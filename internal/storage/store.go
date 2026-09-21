package storage

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var (
	// ErrTooLarge is returned when the upload exceeds the configured limit.
	ErrTooLarge = errors.New("file exceeds maximum upload size")
	// ErrUnsupportedKind is returned when the content is not an allowed local file type.
	ErrUnsupportedKind = errors.New("unsupported file type: only local PDF and PNG files are accepted")
)

// Store persists uploaded blobs under a server-controlled root. Callers never
// supply the on-disk path; the store generates it.
type Store struct {
	root     string
	absRoot  string
	maxBytes int64
}

// Kind describes a validated file type.
type Kind struct {
	Name string
	Ext  string
}

var (
	pdfMagic = []byte("%PDF-")
	pngMagic = []byte{0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A}
)

// New opens (creating if needed) the storage root and returns a Store.
func New(root string, maxBytes int64) (*Store, error) {
	abs, err := filepath.Abs(filepath.Clean(root))
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(abs, 0o750); err != nil {
		return nil, fmt.Errorf("create storage root: %w", err)
	}
	return &Store{root: root, absRoot: abs, maxBytes: maxBytes}, nil
}

// MaxBytes returns the configured upload limit.
func (s *Store) MaxBytes() int64 { return s.maxBytes }

// SaveResult carries the outcome of a successful Save.
type SaveResult struct {
	RelPath string // server-generated path relative to root
	Kind    Kind
	Size    int64
	SHA256  string
}

// detectKind inspects the magic bytes of the upload. Only PDF and PNG are
// accepted; no OCR or content parsing is performed.
func detectKind(head []byte) (Kind, bool) {
	if len(head) >= len(pdfMagic) && bytesEqual(head[:len(pdfMagic)], pdfMagic) {
		return Kind{Name: "pdf", Ext: ".pdf"}, true
	}
	if len(head) >= len(pngMagic) && bytesEqual(head[:len(pngMagic)], pngMagic) {
		return Kind{Name: "png", Ext: ".png"}, true
	}
	return Kind{}, false
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// Save streams r to a freshly generated path, enforcing the size limit, magic
// bytes and SHA-256 in a single pass. A returned error guarantees no file is
// left behind; on success the caller owns the file until Commit or Discard.
func (s *Store) Save(r io.Reader, jobID uint) (*SaveResult, error) {
	shard := fmt.Sprintf("job-%d", jobID)
	dir := filepath.Join(s.absRoot, shard)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("create shard directory: %w", err)
	}

	var name [16]byte
	if _, err := rand.Read(name[:]); err != nil {
		return nil, err
	}
	// .part guarantees a crash before finalization is never mistaken for a
	// committed blob by the startup sweeper.
	tmpName := hex.EncodeToString(name[:]) + ".part"
	tmpPath := filepath.Join(dir, tmpName)

	f, err := os.OpenFile(tmpPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o640)
	if err != nil {
		return nil, err
	}

	h := sha256.New()
	// Limit one extra byte so an over-limit stream is detectable.
	lr := io.LimitReader(r, s.maxBytes+1)
	head := make([]byte, 0, 16)
	buf := make([]byte, 32*1024)
	var size int64
	cleanup := true
	defer func() {
		if cleanup {
			f.Close()
			_ = os.Remove(tmpPath)
		}
	}()

	for {
		n, readErr := lr.Read(buf)
		if n > 0 {
			if size+int64(n) > s.maxBytes {
				return nil, ErrTooLarge
			}
			if len(head) < 16 {
				head = append(head, buf[:min(n, 16-len(head))]...)
			}
			if _, err := f.Write(buf[:n]); err != nil {
				return nil, err
			}
			h.Write(buf[:n])
			size += int64(n)
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return nil, readErr
		}
	}

	if size < 8 {
		return nil, ErrUnsupportedKind
	}
	kind, ok := detectKind(head)
	if !ok {
		return nil, ErrUnsupportedKind
	}

	finalName := hex.EncodeToString(name[:]) + kind.Ext
	finalPath := filepath.Join(dir, finalName)
	if err := f.Sync(); err != nil {
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	if err := os.Rename(tmpPath, finalPath); err != nil {
		return nil, err
	}
	cleanup = false

	rel := filepath.ToSlash(filepath.Join(shard, finalName))
	return &SaveResult{
		RelPath: rel,
		Kind:    kind,
		Size:    size,
		SHA256:  hex.EncodeToString(h.Sum(nil)),
	}, nil
}

// Resolve maps a store-relative path to an absolute on-disk path,
// rejecting any attempt to escape the root (path traversal, absolute or
// NUL-laden paths).
func (s *Store) Resolve(rel string) (string, error) {
	if rel == "" {
		return "", errors.New("empty storage path")
	}
	if filepath.IsAbs(rel) || strings.Contains(rel, "\x00") {
		return "", errors.New("invalid storage path")
	}
	// Join cleans the result AND anchors it under root; cleaning rel alone
	// first would let a later Join collapse traversal segments back in.
	abs, err := filepath.Abs(filepath.Join(s.absRoot, rel))
	if err != nil {
		return "", err
	}
	if !within(abs, s.absRoot) {
		return "", errors.New("storage path escapes root")
	}
	if abs == s.absRoot {
		return "", errors.New("storage path is the root")
	}
	return abs, nil
}

func within(path, root string) bool {
	if path == root {
		return true
	}
	return strings.HasPrefix(path, root+string(os.PathSeparator))
}

// Open validates and opens a committed blob for reading.
func (s *Store) Open(rel string) (*os.File, error) {
	abs, err := s.Resolve(rel)
	if err != nil {
		return nil, err
	}
	return os.Open(abs)
}

// Discard removes a saved blob after a downstream failure (e.g. DB insert).
// A missing file is not an error.
func (s *Store) Discard(rel string) error {
	abs, err := s.Resolve(rel)
	if err != nil {
		return err
	}
	if err := os.Remove(abs); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// SweepOrphans removes leftover .part files (interrupted uploads) and any
// blobs not present in knownPaths (files whose DB record never committed).
func (s *Store) SweepOrphans(knownPaths map[string]struct{}) (partials, blobs int, err error) {
	walkErr := filepath.Walk(s.absRoot, func(path string, info os.FileInfo, werr error) error {
		if werr != nil {
			return werr
		}
		if info.IsDir() {
			return nil
		}
		rel, rerr := filepath.Rel(s.absRoot, path)
		if rerr != nil {
			return rerr
		}
		rel = filepath.ToSlash(rel)
		if strings.HasSuffix(rel, ".part") {
			if err := os.Remove(path); err == nil {
				partials++
			}
			return nil
		}
		if knownPaths != nil {
			if _, ok := knownPaths[rel]; !ok {
				if err := os.Remove(path); err == nil {
					blobs++
				}
			}
		}
		return nil
	})
	return partials, blobs, walkErr
}
