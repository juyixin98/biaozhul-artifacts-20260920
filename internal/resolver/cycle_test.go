package resolver_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/resolver"
	"github.example.com/ocimultipick/internal/selector"
)

// fakeContentStore is an in-memory ContentStore used to exercise behaviors
// that a genuine content-addressable store cannot reach by construction.
//
// When strict is true it enforces the ContentStore contract exactly (a claimed
// digest must equal the digest of the returned bytes), like the on-disk
// blobstore. When strict is false it simulates a buggy/compromised store that
// serves one blob's bytes under a different claimed digest but still reports
// the real identity digest — used to prove the resolver's cycle detection keys
// on content identity rather than on the claimed string.
type fakeContentStore struct {
	blobs       map[string][]byte // claimed digest -> bytes
	sizes       map[string]int64  // claimed digest -> declared size
	strict      bool
	verifySizes bool
}

func (f *fakeContentStore) StatSize(_ string, d digest.Digest) (int64, error) {
	b, ok := f.blobs[d.String()]
	if !ok {
		return 0, fmt.Errorf("%w: %s", blobstore.ErrNotFound, d)
	}
	if n, ok := f.sizes[d.String()]; ok {
		return n, nil
	}
	return int64(len(b)), nil
}

func (f *fakeContentStore) ReadVerified(_ string, desc oci.Descriptor) (string, []byte, error) {
	body, ok := f.blobs[desc.Digest]
	if !ok {
		return "", nil, fmt.Errorf("%w: %s", blobstore.ErrNotFound, desc.Digest)
	}
	real, err := digest.FromBytes("sha256", body)
	if err != nil {
		return "", nil, err
	}
	if f.strict && real.String() != desc.Digest {
		return "", nil, fmt.Errorf("%w: claimed %s but bytes are %s", digest.ErrMismatch, desc.Digest, real)
	}
	if f.verifySizes && int64(len(body)) != desc.Size {
		return "", nil, fmt.Errorf("%w: declared %d actual %d",
			digest.ErrSizeMismatch, desc.Size, len(body))
	}
	// Always report the true content identity, even for the buggy store.
	return real.String(), body, nil
}

func indexBlob(t *testing.T, children ...oci.Descriptor) []byte {
	t.Helper()
	idx := oci.Index{SchemaVersion: 2, Manifests: children}
	raw, err := json.Marshal(&idx)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// mutualCycle builds two indexes that reference each other and stores each
// blob's bytes under the *other's* claimed digest, modelling an attempt to
// alias content into a cycle. Declared sizes are made truthful by iterating
// to the (quickly converging) length fixed point, so verification reaches the
// alias/cycle logic rather than failing earlier on a size error.
func mutualCycle(t *testing.T) (store *fakeContentStore, rootA, rootB string) {
	const a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const b = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

	var bytesA, bytesB []byte
	sizeA, sizeB := int64(0), int64(0)
	for i := 0; i < 10; i++ {
		bytesA = indexBlob(t, oci.Descriptor{Digest: b, Size: sizeB})
		bytesB = indexBlob(t, oci.Descriptor{Digest: a, Size: sizeA})
		newA, newB := int64(len(bytesA)), int64(len(bytesB))
		if newA == sizeA && newB == sizeB {
			break
		}
		sizeA, sizeB = newA, newB
	}

	st := &fakeContentStore{blobs: map[string][]byte{a: bytesA, b: bytesB}}
	return st, a, b
}

func TestStrictStoreRejectsAliasedCycleAttempt(t *testing.T) {
	st, rootA, _ := mutualCycle(t)
	st.strict = true
	st.verifySizes = true
	rootD, _ := digest.Parse(rootA)

	_, err := resolver.New(st).Resolve("repo", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeDigestMismatch {
		t.Fatalf("a correctly verified content store must reject aliased bytes with digest_mismatch, got %v", err)
	}
}

func TestCycleDetectedAgainstBuggyStore(t *testing.T) {
	st, rootA, _ := mutualCycle(t)
	st.strict = false // simulate a store that serves aliased bytes but reports identity
	st.verifySizes = true
	rootD, _ := digest.Parse(rootA)

	_, err := resolver.New(st).Resolve("repo", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeCycleDetected {
		t.Fatalf("identity-keyed cycle detection must fire, got %v", err)
	}
}

func TestSelfReferenceIsACycle(t *testing.T) {
	const self = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	// A self-referencing index: its sole child descriptor points back to itself.
	body := indexBlob(t, oci.Descriptor{Digest: self, Size: 0})
	st := &fakeContentStore{
		blobs:       map[string][]byte{self: body},
		strict:      false,
		verifySizes: false, // focus on the alias/cycle defense, not size
	}
	rootD, _ := digest.Parse(self)
	_, err := resolver.New(st).Resolve("repo", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeCycleDetected {
		t.Fatalf("self reference must be reported as a cycle, got %v", err)
	}
}
