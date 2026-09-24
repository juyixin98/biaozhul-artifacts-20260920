// Package blobstore stores OCI content in local OCI image-layout directories.
//
// Each repository lives at <root>/<repository>/ and contains the standard
// layout:
//
//	<root>/<repository>/oci-layout
//	<root>/<repository>/index.json
//	<root>/<repository>/blobs/sha256/<hex>
//
// No network access is performed: only blobs present locally can be resolved.
package blobstore

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
)

// ErrNotFound indicates a blob or repository does not exist.
var ErrNotFound = errors.New("blob not found")

var (
	repoSegment = regexp.MustCompile(`^[a-z0-9]+(?:[._-][a-z0-9]+)*$`)
	tagPattern  = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$`)
)

// Store is a filesystem-backed OCI content store rooted at a directory.
type Store struct {
	root string
}

// New opens (and lazily creates repositories within) a store rooted at root.
func New(root string) *Store {
	return &Store{root: root}
}

// Root returns the store root directory.
func (s *Store) Root() string { return s.root }

// ValidRepo validates a repository name.
func ValidRepo(repo string) bool {
	if repo == "" || strings.HasPrefix(repo, "/") || strings.HasSuffix(repo, "/") {
		return false
	}
	for _, seg := range strings.Split(repo, "/") {
		if !repoSegment.MatchString(seg) {
			return false
		}
	}
	return true
}

// ValidTag validates a tag name.
func ValidTag(tag string) bool { return tagPattern.MatchString(tag) }

func (s *Store) repoDir(repo string) (string, error) {
	if !ValidRepo(repo) {
		return "", fmt.Errorf("invalid repository name %q", repo)
	}
	clean := filepath.Clean(filepath.Join(s.root, filepath.FromSlash(repo)))
	if !strings.HasPrefix(clean+string(os.PathSeparator), filepath.Clean(s.root)+string(os.PathSeparator)) {
		return "", fmt.Errorf("repository path escapes store root: %q", repo)
	}
	return clean, nil
}

func blobRelPath(d digest.Digest) string {
	return filepath.Join("blobs", d.Algorithm(), d.Encoded())
}

// EnsureRepo creates the repository layout if it does not exist.
func (s *Store) EnsureRepo(repo string) error {
	dir, err := s.repoDir(repo)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(dir, "blobs", "sha256"), 0o755); err != nil {
		return err
	}
	layout := filepath.Join(dir, oci.LayoutFile)
	if _, err := os.Stat(layout); errors.Is(err, os.ErrNotExist) {
		content := []byte(fmt.Sprintf(`{"imageLayoutVersion":%q}%s`, oci.LayoutVersion, "\n"))
		if werr := os.WriteFile(layout, content, 0o644); werr != nil {
			return werr
		}
	}
	idx := filepath.Join(dir, "index.json")
	if _, err := os.Stat(idx); errors.Is(err, os.ErrNotExist) {
		if werr := os.WriteFile(idx, []byte(`{"schemaVersion":2,"manifests":[]}`+"\n"), 0o644); werr != nil {
			return werr
		}
	}
	return nil
}

// ListRepos returns the repository names that exist under the root.
func (s *Store) ListRepos() ([]string, error) {
	var repos []string
	err := filepath.WalkDir(s.root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || d.Name() != oci.LayoutFile {
			return nil
		}
		rel, err := filepath.Rel(s.root, filepath.Dir(path))
		if err != nil {
			return err
		}
		if rel != "." {
			repos = append(repos, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	sort.Strings(repos)
	return repos, nil
}

// Put hashes r using algorithm, writes the content into the repository and
// returns a descriptor of the written blob. Existing identical blobs are not
// rewritten.
func (s *Store) Put(repo, algorithm, mediaType string, r io.Reader) (oci.Descriptor, error) {
	dir, err := s.repoDir(repo)
	if err != nil {
		return oci.Descriptor{}, err
	}
	if err := s.EnsureRepo(repo); err != nil {
		return oci.Descriptor{}, err
	}
	var buf bytes.Buffer
	tee := io.TeeReader(r, &buf)
	d, n, err := digest.FromReader(algorithm, tee)
	if err != nil {
		return oci.Descriptor{}, err
	}
	final := filepath.Join(dir, blobRelPath(d))
	if _, statErr := os.Stat(final); errors.Is(statErr, os.ErrNotExist) {
		tmp := final + ".tmp"
		if err := os.WriteFile(tmp, buf.Bytes(), 0o644); err != nil {
			return oci.Descriptor{}, err
		}
		if err := os.Rename(tmp, final); err != nil {
			return oci.Descriptor{}, err
		}
	}
	return oci.Descriptor{MediaType: mediaType, Digest: d.String(), Size: n}, nil
}

// PutBytes is a convenience wrapper around Put for in-memory content.
func (s *Store) PutBytes(repo, algorithm, mediaType string, content []byte) (oci.Descriptor, error) {
	return s.Put(repo, algorithm, mediaType, bytes.NewReader(content))
}

// Open opens a blob for reading. The caller must close the reader.
func (s *Store) Open(repo string, d digest.Digest) (io.ReadCloser, int64, error) {
	dir, err := s.repoDir(repo)
	if err != nil {
		return nil, 0, err
	}
	f, err := os.Open(filepath.Join(dir, blobRelPath(d)))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, 0, fmt.Errorf("%w: %s", ErrNotFound, d)
		}
		return nil, 0, err
	}
	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	return f, fi.Size(), nil
}

// Verify reads the blob identified by desc and verifies both its digest and
// its declared size against the actual bytes on disk.
func (s *Store) Verify(repo string, desc oci.Descriptor) error {
	d, err := digest.Parse(desc.Digest)
	if err != nil {
		return err
	}
	r, _, err := s.Open(repo, d)
	if err != nil {
		return err
	}
	defer r.Close()
	if _, err := digest.VerifyReader(d, desc.Size, r); err != nil {
		return fmt.Errorf("verifying %s: %w", desc.Digest, err)
	}
	return nil
}

// ReadJSON verifies the blob's digest and size and JSON-decodes it into v.
func (s *Store) ReadJSON(repo string, desc oci.Descriptor, v any) error {
	_, b, err := s.ReadVerified(repo, desc)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("decoding blob %s: %w", desc.Digest, err)
	}
	return nil
}

// ReadVerified reads the blob, checks its digest and declared size, and
// returns the digest the bytes actually hash to (which equals the claimed
// digest on success) and the full content.
func (s *Store) ReadVerified(repo string, desc oci.Descriptor) (string, []byte, error) {
	d, err := digest.Parse(desc.Digest)
	if err != nil {
		return "", nil, err
	}
	r, _, err := s.Open(repo, d)
	if err != nil {
		return "", nil, err
	}
	defer r.Close()
	var buf bytes.Buffer
	if _, err := digest.VerifyReader(d, desc.Size, io.TeeReader(r, &buf)); err != nil {
		return "", nil, err
	}
	return d.String(), buf.Bytes(), nil
}

// StatSize returns the on-disk size of a blob.
func (s *Store) StatSize(repo string, d digest.Digest) (int64, error) {
	r, size, err := s.Open(repo, d)
	if err != nil {
		return 0, err
	}
	_ = r.Close()
	return size, nil
}

// Has reports whether the blob exists locally.
func (s *Store) Has(repo string, d digest.Digest) (bool, error) {
	dir, err := s.repoDir(repo)
	if err != nil {
		return false, err
	}
	_, err = os.Stat(filepath.Join(dir, blobRelPath(d)))
	switch {
	case err == nil:
		return true, nil
	case errors.Is(err, os.ErrNotExist):
		return false, nil
	default:
		return false, err
	}
}

// Remove deletes a blob from a repository. Used by tests/fixtures to
// simulate missing content.
func (s *Store) Remove(repo string, d digest.Digest) error {
	dir, err := s.repoDir(repo)
	if err != nil {
		return err
	}
	err = os.Remove(filepath.Join(dir, blobRelPath(d)))
	if errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("%w: %s", ErrNotFound, d)
	}
	return err
}

// LoadIndex reads and decodes a repository's index.json.
func (s *Store) LoadIndex(repo string) (*oci.Index, error) {
	if err := s.EnsureRepo(repo); err != nil {
		return nil, err
	}
	dir, err := s.repoDir(repo)
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err != nil {
		return nil, err
	}
	var idx oci.Index
	if err := json.Unmarshal(b, &idx); err != nil {
		return nil, fmt.Errorf("parsing index.json of %s: %w", repo, err)
	}
	return &idx, nil
}

// SaveIndex writes the repository index.json atomically.
func (s *Store) SaveIndex(repo string, idx *oci.Index) error {
	dir, err := s.repoDir(repo)
	if err != nil {
		return err
	}
	b, err := json.MarshalIndent(idx, "", "  ")
	if err != nil {
		return err
	}
	final := filepath.Join(dir, "index.json")
	tmp := final + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, final)
}
