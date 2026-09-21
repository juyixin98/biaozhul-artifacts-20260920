package securefile

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestResolveWhitelistAndSymlinks(t *testing.T) {
	root := t.TempDir()
	vault := filepath.Join(root, "vault")
	outside := filepath.Join(root, "outside")
	mustMkdir(t, vault)
	mustMkdir(t, outside)

	// Prepare a legitimate image.
	good := filepath.Join(vault, "disk.dd")
	mustWrite(t, good, make([]byte, 1024))

	// A file outside the vault.
	bad := filepath.Join(outside, "evil.dd")
	mustWrite(t, bad, make([]byte, 1024))

	// A directory traversal attempt using ../ .
	traversal := filepath.Join(vault, "sub", "..", "..", "outside", "evil.dd")
	mustMkdir(t, filepath.Join(vault, "sub"))

	// A symlink inside the vault pointing outside.
	link := filepath.Join(vault, "link.dd")
	if err := os.Symlink(bad, link); err != nil {
		t.Fatal(err)
	}

	// An intermediate-directory symlink escape.
	subLink := filepath.Join(vault, "escape-dir")
	if err := os.Symlink(outside, subLink); err != nil {
		t.Fatal(err)
	}
	throughDir := filepath.Join(subLink, "evil.dd")

	r := NewResolver([]string{vault})

	t.Run("legitimate file opens", func(t *testing.T) {
		f, err := r.Resolve(good)
		if err != nil {
			t.Fatalf("expected open, got %v", err)
		}
		defer f.Close()
		if f.Identity().RealPath != good {
			t.Fatalf("real path = %q, want %q", f.Identity().RealPath, good)
		}
		if f.Identity().DeviceID == 0 || f.Identity().Inode == 0 {
			t.Fatalf("identity must capture device/inode, got %+v", f.Identity())
		}
	})

	t.Run("relative traversal rejected", func(t *testing.T) {
		if _, err := r.Resolve(traversal); !errors.Is(err, ErrOutsideWhitelist) {
			t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
		}
	})

	t.Run("absolute outside rejected", func(t *testing.T) {
		if _, err := r.Resolve(bad); !errors.Is(err, ErrOutsideWhitelist) {
			t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
		}
	})

	t.Run("symlink to outside rejected", func(t *testing.T) {
		if _, err := r.Resolve(link); !errors.Is(err, ErrOutsideWhitelist) {
			t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
		}
	})

	t.Run("symlinked directory escape rejected", func(t *testing.T) {
		if _, err := r.Resolve(throughDir); !errors.Is(err, ErrOutsideWhitelist) {
			t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
		}
	})

	t.Run("wrong extension rejected", func(t *testing.T) {
		other := filepath.Join(vault, "notes.txt")
		mustWrite(t, other, []byte("x"))
		if _, err := r.Resolve(other); !errors.Is(err, ErrUnsupportedType) {
			t.Fatalf("expected ErrUnsupportedType, got %v", err)
		}
	})

	t.Run("directory rejected", func(t *testing.T) {
		if _, err := r.Resolve(vault); !errors.Is(err, ErrUnsupportedType) {
			// vault itself lacks extension, so UnsupportedType comes first;
			// point it at a .dd dir to test regular-file check.
			ddDir := filepath.Join(vault, "folder.dd")
			mustMkdir(t, ddDir)
			if _, err := r.Resolve(ddDir); !errors.Is(err, ErrNotRegularFile) {
				t.Fatalf("expected ErrNotRegularFile, got %v", err)
			}
		}
	})

	t.Run("missing file errors", func(t *testing.T) {
		if _, err := r.Resolve(filepath.Join(vault, "nope.dd")); err == nil {
			t.Fatal("expected error for missing file")
		}
	})
}

// TestPathPrefixBoundary ensures /vault-evil is not treated as inside /vault.
func TestPathPrefixBoundary(t *testing.T) {
	root := t.TempDir()
	vault := filepath.Join(root, "vault")
	lookalike := vault + "-evil"
	mustMkdir(t, vault)
	mustMkdir(t, lookalike)
	img := filepath.Join(lookalike, "x.dd")
	mustWrite(t, img, []byte("data"))

	r := NewResolver([]string{vault})
	if _, err := r.Resolve(img); !errors.Is(err, ErrOutsideWhitelist) {
		t.Fatalf("prefix lookalike must be rejected, got %v", err)
	}
}

// TestHashAndVerifyChangedDuringRead mutates the file from the chunk hook and
// requires ErrFileChanged instead of a digest for a moving target.
func TestHashAndVerifyChangedDuringRead(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "moving.dd")
	original := bytesFill(4096*8, 0xAA)
	if err := os.WriteFile(path, original, 0o644); err != nil {
		t.Fatal(err)
	}

	r := NewResolver([]string{dir})
	f, err := r.Resolve(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	var once sync.Once
	_, err = f.HashAndVerify(4096, func(index int, _, _ int64) {
		// Mutate a few chunks in. The tiny test file reads in microseconds;
		// sleep briefly so the rewrite lands in a strictly later clock tick
		// (a real multi-GB registration has a wide natural window).
		if index == 3 {
			once.Do(func() {
				time.Sleep(2 * time.Millisecond)
				tweaked := bytesFill(4096*8, 0xBB)
				if err := os.WriteFile(path, tweaked, 0o644); err != nil {
					t.Errorf("mutate: %v", err)
				}
			})
		}
	})
	if !errors.Is(err, ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged, got %v", err)
	}
}

// TestHashAndVerifyStable hashes an unchanged file and compares to a direct
// sha256 computed independently.
func TestHashAndVerifyStable(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "stable.dd")
	data := bytesFill(4096*3+17, 0x43) // deliberately unaligned tail
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
	r := NewResolver([]string{dir})
	f, err := r.Resolve(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	got, err := f.HashAndVerify(4096, nil)
	if err != nil {
		t.Fatalf("stable file hash: %v", err)
	}
	sum := sha256.Sum256(data)
	if want := hex.EncodeToString(sum[:]); got != want {
		t.Fatalf("hash mismatch: got %s want %s", got, want)
	}
}

// TestShrinkDuringRead ensures a truncating file fails rather than hashing a
// prefix silently.
func TestShrinkDuringRead(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "shrink.dd")
	if err := os.WriteFile(path, bytesFill(4096*8, 0x11), 0o644); err != nil {
		t.Fatal(err)
	}
	r := NewResolver([]string{dir})
	f, err := r.Resolve(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var once sync.Once
	_, err = f.HashAndVerify(4096, func(index int, _, _ int64) {
		if index == 3 {
			once.Do(func() {
				time.Sleep(2 * time.Millisecond)
				if err := os.Truncate(path, 4096); err != nil {
					t.Errorf("truncate: %v", err)
				}
			})
		}
	})
	if err == nil {
		t.Fatal("expected error for shrinking file")
	}
	if !errors.Is(err, ErrFileChanged) && !strings.Contains(err.Error(), "changed") {
		t.Fatalf("expected change error, got %v", err)
	}
}

func mustMkdir(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(p, 0o755); err != nil {
		t.Fatal(err)
	}
}

func mustWrite(t *testing.T, p string, b []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func bytesFill(n int, b byte) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}
