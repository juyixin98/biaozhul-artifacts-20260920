// Package safeio performs read-only, path-constrained hashing of evidence images.
//
// It defends against:
//   - path traversal (../), absolute paths, NUL bytes in the requested name;
//   - symlink escapes: the candidate is resolved with filepath.EvalSymlinks
//     and the result must stay inside the whitelist root;
//   - non-regular files (devices, directories, pipes);
//   - mutation during reading: size/mtime/inode are checked from the same
//     open file descriptor before and after streaming, and every Read is
//     length-checked against the recorded size.
package safeio

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

var (
	// ErrOutsideWhitelist means the requested file escapes the evidence root.
	ErrOutsideWhitelist = errors.New("path is outside the evidence whitelist directory")
	// ErrIllegalName means the requested name cannot be used.
	ErrIllegalName = errors.New("illegal file name")
	// ErrNotRegular means the target is not a regular file.
	ErrNotRegular = errors.New("target is not a regular file")
	// ErrFileChanged means the file changed while it was being read.
	ErrFileChanged = errors.New("file changed during reading")
)

// Identity is the verified identity of an evidence image.
type Identity struct {
	Path       string // absolute, symlink-resolved path actually opened
	Size       int64
	MtimeNanos int64
	Device     uint64
	Inode      uint64
	Mode       fs.FileMode
}

func statIdentity(path string, fi fs.FileInfo) Identity {
	var dev, ino uint64
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		dev = uint64(st.Dev)
		ino = st.Ino
	}
	return Identity{
		Path:       path,
		Size:       fi.Size(),
		MtimeNanos: fi.ModTime().UnixNano(),
		Device:     dev,
		Inode:      ino,
		Mode:       fi.Mode(),
	}
}

// validateName rejects obviously dangerous requested names before touching disk.
func validateName(name string) error {
	if name == "" {
		return fmt.Errorf("%w: empty name", ErrIllegalName)
	}
	if strings.ContainsRune(name, 0) {
		return fmt.Errorf("%w: NUL byte", ErrIllegalName)
	}
	if filepath.IsAbs(name) {
		return fmt.Errorf("%w: absolute paths are not allowed", ErrIllegalName)
	}
	// Reject Windows-style drive / UNC leftovers for defense in depth.
	if strings.Contains(name, ":") {
		return fmt.Errorf("%w: ':' is not allowed in file names", ErrIllegalName)
	}
	cleaned := filepath.Clean(name)
	if cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) {
		return fmt.Errorf("%w: parent directory traversal", ErrOutsideWhitelist)
	}
	for _, part := range strings.Split(filepath.ToSlash(cleaned), "/") {
		if part == ".." {
			return fmt.Errorf("%w: parent directory traversal", ErrOutsideWhitelist)
		}
	}
	return nil
}

// Resolve checks name against root and returns the verified absolute path
// after symlink resolution, plus its identity. It never follows a symlink to
// a location outside root.
func Resolve(root, name string) (string, Identity, error) {
	var zero Identity
	if err := validateName(name); err != nil {
		return "", zero, err
	}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", zero, err
	}
	resolvedRoot, err := filepath.EvalSymlinks(absRoot)
	if err != nil {
		return "", zero, fmt.Errorf("evidence root unavailable: %w", err)
	}
	candidate := filepath.Join(absRoot, name)
	// Resolve every symlink component of the candidate.
	resolved, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		return "", zero, err
	}
	rel, err := filepath.Rel(resolvedRoot, resolved)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", zero, fmt.Errorf("%w: %s", ErrOutsideWhitelist, name)
	}
	fi, err := os.Stat(resolved)
	if err != nil {
		return "", zero, err
	}
	if !fi.Mode().IsRegular() {
		return "", zero, fmt.Errorf("%w: %s", ErrNotRegular, name)
	}
	return resolved, statIdentity(resolved, fi), nil
}

// OpenVerify resolves name under root and opens it O_RDONLY, confirming that
// the directory entry and the opened descriptor describe the same inode. This
// closes the TOCTOU window where the path is swapped between Resolve and Open.
func OpenVerify(root, name string) (*os.File, Identity, error) {
	resolved, id, err := Resolve(root, name)
	if err != nil {
		return nil, Identity{}, err
	}
	f, err := os.OpenFile(resolved, os.O_RDONLY, 0)
	if err != nil {
		return nil, Identity{}, err
	}
	fi, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return nil, Identity{}, err
	}
	openedID := statIdentity(resolved, fi)
	if openedID.Inode != id.Inode || openedID.Device != id.Device || openedID.Size != id.Size {
		_ = f.Close()
		return nil, Identity{}, fmt.Errorf("%w: path was swapped", ErrFileChanged)
	}
	if !fi.Mode().IsRegular() {
		_ = f.Close()
		return nil, Identity{}, fmt.Errorf("%w: %s", ErrNotRegular, name)
	}
	return f, openedID, nil
}

// AfterChunk is invoked after each chunk has been hashed; it exists mainly for
// tests that need to mutate the file mid-read. byteOffset is the running total.
type AfterChunk func(byteOffset int64) error

// HashOptions controls HashInChunks.
type HashOptions struct {
	ChunkSize int
	// Offset starts reading here (used by resumable verification).
	Offset int64
	// ExpectedSize, when nonzero, must match the descriptor's size.
	ExpectedSize int64
	// VerifyReadback, when true, re-reads each chunk immediately after the
	// first read and compares it byte-for-byte. This catches in-place writes
	// that happen between the two reads without relying on filesystem mtime
	// resolution. Registration uses it so a changing image can never produce
	// a stored baseline.
	VerifyReadback bool
	AfterChunk     AfterChunk
}

// HashInChunks streams the (already open) file in read-only chunks and returns
// the SHA-256 over bytes [Offset, Size). Per-chunk digests are returned so a
// caller can persist them for resume-time tamper checking.
//
// The file's identity (size/mtime/inode) is re-checked from the same
// descriptor when reading finishes: a replacement, growth, shrink or an
// in-place write that changes mtime mid-read fails the call. With
// VerifyReadback the whole stream is hashed a second time (identity is
// re-proved first): registration uses this so that any change during
// processing — even a same-tick write that leaves mtime unchanged — fails the
// call and can never produce a stored baseline.
func HashInChunks(f *os.File, id Identity, opts HashOptions) (string, []string, error) {
	digest, chunks, fi, err := hashPass(f, id, opts)
	if err != nil {
		return "", nil, err
	}
	if !opts.VerifyReadback {
		return digest, chunks, nil
	}
	// Second full pass. Identity must still match, and the digest must equal
	// the first pass: this brackets the entire reading interval.
	pass2 := HashOptions{ChunkSize: opts.ChunkSize, Offset: opts.Offset, ExpectedSize: fi.Size}
	digest2, _, fi2, err := hashPass(f, id, pass2)
	if err != nil {
		return "", nil, err
	}
	if digest2 != digest || fi2.Inode != fi.Inode || fi2.MtimeNanos != fi.MtimeNanos || fi2.Size != fi.Size {
		return "", nil, fmt.Errorf("%w: file content changed between read passes", ErrFileChanged)
	}
	return digest, chunks, nil
}

// hashPass performs one streaming pass and returns the digest, chunk digests
// and the post-pass file identity.
func hashPass(f *os.File, id Identity, opts HashOptions) (string, []string, Identity, error) {
	var zero Identity
	if opts.ChunkSize <= 0 {
		return "", nil, zero, fmt.Errorf("chunk size must be positive")
	}
	if opts.Offset < 0 || (opts.ExpectedSize > 0 && opts.Offset > opts.ExpectedSize) {
		return "", nil, zero, fmt.Errorf("invalid offset")
	}

	startFi, err := f.Stat()
	if err != nil {
		return "", nil, zero, err
	}
	start := statIdentity(id.Path, startFi)
	if start.Inode != id.Inode || start.Device != id.Device {
		return "", nil, zero, fmt.Errorf("%w: inode/device mismatch", ErrFileChanged)
	}
	if opts.ExpectedSize > 0 && start.Size != opts.ExpectedSize {
		return "", nil, zero, fmt.Errorf("%w: size changed (expected %d, got %d)", ErrFileChanged, opts.ExpectedSize, start.Size)
	}
	if opts.Offset > start.Size {
		return "", nil, zero, fmt.Errorf("%w: offset past end", ErrFileChanged)
	}

	if _, err := f.Seek(opts.Offset, io.SeekStart); err != nil {
		return "", nil, zero, err
	}

	h := sha256.New()
	buf := make([]byte, opts.ChunkSize)
	remaining := start.Size - opts.Offset
	chunkDigests := make([]string, 0)
	var offset int64 = opts.Offset

	for remaining > 0 {
		want := int64(len(buf))
		if remaining < want {
			want = remaining
		}
		n, err := io.ReadFull(f, buf[:want])
		if n > 0 {
			h.Write(buf[:n])
			sum := sha256.Sum256(buf[:n])
			chunkDigests = append(chunkDigests, hex.EncodeToString(sum[:]))
			offset += int64(n)
			remaining -= int64(n)
			if opts.AfterChunk != nil {
				if hErr := opts.AfterChunk(offset); hErr != nil {
					return "", nil, zero, hErr
				}
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				return "", nil, zero, fmt.Errorf("%w: truncated during read", ErrFileChanged)
			}
			return "", nil, zero, err
		}
	}

	endFi, err := f.Stat()
	if err != nil {
		return "", nil, zero, err
	}
	end := statIdentity(id.Path, endFi)
	switch {
	case end.Inode != start.Inode || end.Device != start.Device:
		return "", nil, zero, fmt.Errorf("%w: inode changed", ErrFileChanged)
	case end.Size != start.Size:
		return "", nil, zero, fmt.Errorf("%w: size changed during reading", ErrFileChanged)
	case !end.MtimeEqual(start):
		return "", nil, zero, fmt.Errorf("%w: mtime changed during reading", ErrFileChanged)
	}
	return hex.EncodeToString(h.Sum(nil)), chunkDigests, end, nil
}

// MtimeEqual compares two modification times with second+nanosecond precision.
func (id Identity) MtimeEqual(other Identity) bool {
	return id.MtimeNanos == other.MtimeNanos
}

// ModTime returns the captured mtime.
func (id Identity) ModTime() time.Time { return time.Unix(0, id.MtimeNanos) }
