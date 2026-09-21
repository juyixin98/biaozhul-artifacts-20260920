package securefile

import (
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"io"
	"os"
)

// File is a whitelist-checked, read-only open image with the identity captured
// immediately after open.
type File struct {
	*os.File
	identity Identity
}

// Identity returns the post-open identity snapshot.
func (f *File) Identity() Identity { return f.identity }

// CurrentIdentity fstats the descriptor again.
func (f *File) CurrentIdentity() (Identity, error) {
	id, err := fdIdentity(f.File)
	if err != nil {
		return Identity{}, err
	}
	id.RealPath = f.identity.RealPath
	return id, nil
}

// ChunkHook is invoked after every buffered chunk is fed to the hasher. It
// exists for tests to mutate the file mid-read; production callers pass nil.
type ChunkHook func(chunkIndex int, offset, n int64)

// HashAndVerify streams the open descriptor into a SHA-256 and verifies the
// file did not change between the start and the end of the read. The hook (nil
// in production) fires after each chunk; tests use it to mutate the file
// mid-read. Any size/mtime/ctime/identity delta at the end surfaces as
// ErrFileChanged so callers never persist a baseline built from a moving target.
func (f *File) HashAndVerify(chunkSize int, hook ChunkHook) (string, error) {
	return f.hashAndVerify(chunkSize, hook)
}

// HashAndVerifyQuiet is the production path with no hook.
func (f *File) HashAndVerifyQuiet(chunkSize int) (string, error) {
	return f.hashAndVerify(chunkSize, nil)
}

func (f *File) hashAndVerify(chunkSize int, hook ChunkHook) (string, error) {
	start := f.identity
	if cur, err := f.CurrentIdentity(); err != nil {
		return "", err
	} else if !cur.Equal(start) {
		return "", ErrFileChanged
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return "", err
	}

	h := sha256.New()
	sum, err := hashFull(f.File, h, start.Size, chunkSize, hook)
	if err != nil {
		return "", err
	}
	cur, err := f.CurrentIdentity()
	if err != nil {
		return "", err
	}
	if !cur.Equal(start) {
		return "", ErrFileChanged
	}
	return hex.EncodeToString(sum), nil
}

// hashFull reads exactly expectedSize bytes through h using fixed-size reads,
// so a short read from a growing file is detected rather than silently
// accepted.
func hashFull(r io.Reader, h hash.Hash, expectedSize int64, chunkSize int, hook ChunkHook) ([]byte, error) {
	buf := make([]byte, chunkSize)
	var offset, index int64
	for offset < expectedSize {
		want := int64(len(buf))
		if remaining := expectedSize - offset; remaining < want {
			want = remaining
		}
		n, err := io.ReadFull(r, buf[:want])
		if n > 0 {
			h.Write(buf[:n])
			offset += int64(n)
			if hook != nil {
				hook(int(index), offset-int64(n), int64(n))
			}
			index++
		}
		if err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				// File shrank while being read: identity re-check reports the
				// precise mismatch; surface the change error.
				return nil, ErrFileChanged
			}
			return nil, err
		}
	}
	return h.Sum(nil), nil
}
