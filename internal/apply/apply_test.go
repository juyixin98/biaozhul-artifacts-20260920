package apply

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"deltaupdate/internal/delta"
)

type fakeSpace int64

func (f fakeSpace) AvailableBytes(string) (int64, error) { return int64(f), nil }

func sha(b []byte) string {
	s := sha256.Sum256(b)
	return hexEnc(s[:])
}

func hexEnc(b []byte) string {
	const hexd = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, c := range b {
		out[2*i] = hexd[c>>4]
		out[2*i+1] = hexd[c&0xf]
	}
	return string(out)
}

func makePatch(t *testing.T, oldB, newB []byte, bs int) *delta.Patch {
	t.Helper()
	p, err := delta.Generate(bytes.NewReader(oldB), int64(len(oldB)),
		bytes.NewReader(newB), int64(len(newB)), bs)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func writeTarget(t *testing.T, path string, b []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatal(err)
	}
}

// baseApply is the happy path: old artifact must be replaced atomically and
// verify to NewSum.
func TestApplyHappyPath(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("0123456789ABCDEF"), 1000) // 16000
	newB := append([]byte{}, oldB...)
	newB = append(newB[:3000], append([]byte("### NEW REGION ###"), newB[3100:]...)...)
	newB = append(newB, []byte("TAIL-EXTRA")...)
	writeTarget(t, target, oldB)

	p := makePatch(t, oldB, newB, 512)
	f, _ := os.Open(target)
	res, err := Apply(p, f, target, Options{Space: fakeSpace(1 << 30)})
	f.Close()
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if !res.Applied {
		t.Fatal("expected Applied=true")
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, newB) {
		t.Fatal("target content mismatch")
	}
	if res.NewDigest != sha(newB) {
		t.Fatal("result digest mismatch")
	}
	if _, err := os.Stat(target + ".delta.tmp"); !os.IsNotExist(err) {
		t.Fatal("temp file left behind")
	}
}

// TestApplyWrongBase: a patch generated from another base must be rejected and
// the on-disk artifact must remain untouched.
func TestApplyWrongBase(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldA := bytes.Repeat([]byte("A-CONTENT-0001"), 800)
	oldB := bytes.Repeat([]byte("B-DIFFERENT-002"), 800)
	newB := bytes.Repeat([]byte("B-DIFFERENT-002"), 801)
	writeTarget(t, target, oldA) // disk has A

	p := makePatch(t, oldB, newB, 256) // patch expects B -> newB
	before, _ := os.ReadFile(target)
	f, _ := os.Open(target)
	_, err := Apply(p, f, target, Options{Space: fakeSpace(1 << 30)})
	f.Close()
	if !errors.Is(err, ErrWrongBase) {
		t.Fatalf("want ErrWrongBase, got %v", err)
	}
	after, _ := os.ReadFile(target)
	if !bytes.Equal(before, after) {
		t.Fatal("wrong-base apply mutated the target")
	}
}

// TestApplyCorruptPatch: tamper with an op after binding; patchSum must reject.
func TestApplyCorruptPatch(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("X-OLD-BYTES"), 500)
	newB := bytes.Repeat([]byte("Y-NEW-BYTES"), 500)
	writeTarget(t, target, oldB)
	p := makePatch(t, oldB, newB, 128)

	// Tamper: flip a literal byte in one data op, or nudge NewSum.
	tampered := *p
	tampered.NewSum = sha([]byte("not the new content"))
	// patchSum stays the original -> mismatch.
	f, _ := os.Open(target)
	_, err := Apply(&tampered, f, target, Options{Space: fakeSpace(1 << 30)})
	f.Close()
	if !errors.Is(err, ErrCorruptPatch) {
		t.Fatalf("want ErrCorruptPatch for tampered NewSum, got %v", err)
	}

	// Tamper op payload but leave metadata: must fail either patchSum
	// verification (pre-write) or verify-delta; never apply corrupt content.
	p2 := makePatch(t, oldB, newB, 128)
	raw, _ := json.Marshal(p2)
	var decoded map[string]any
	json.Unmarshal(raw, &decoded)
	ops := decoded["ops"].([]any)
	for _, o := range ops {
		om := o.(map[string]any)
		if d, ok := om["data"].(string); ok && d != "" {
			om["data"] = "AA" + d[2:]
			break
		}
	}
	tamperedRaw, _ := json.Marshal(decoded)
	var bad delta.Patch
	json.Unmarshal(tamperedRaw, &bad)
	f2, _ := os.Open(target)
	_, err2 := Apply(&bad, f2, target, Options{Space: fakeSpace(1 << 30)})
	f2.Close()
	if err2 == nil {
		t.Fatal("tampered op payload applied without error")
	}
	before, _ := os.ReadFile(target)
	if !bytes.Equal(before, oldB) {
		t.Fatal("target mutated by corrupt patch attempt")
	}
}

// TestApplyNoSpace: preflight refusal leaves old artifact intact.
func TestApplyNoSpace(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := make([]byte, 4096)
	newB := make([]byte, 2_000_000)
	for i := range newB {
		newB[i] = byte(i)
	}
	writeTarget(t, target, oldB)
	p := makePatch(t, oldB, newB, 512)
	f, _ := os.Open(target)
	_, err := Apply(p, f, target, Options{Space: fakeSpace(10)}) // nearly 0 free
	f.Close()
	if !errors.Is(err, ErrNoSpace) {
		t.Fatalf("want ErrNoSpace, got %v", err)
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, oldB) {
		t.Fatal("target mutated despite space refusal")
	}
	if _, serr := os.Stat(target + ".delta.tmp"); !os.IsNotExist(serr) {
		t.Fatal("temp left behind after space refusal")
	}
}

// TestApplyDoesNotMutateOldWhileReading: while applying, the read handle must
// observe stable old bytes; final content must be new. Also assert the old
// file's inode changes only via rename, content never overwritten in place.
func TestApplyOldReadHandleStable(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("stable-old-"), 900)
	newB := bytes.Repeat([]byte("fresh-new--"), 900)
	writeTarget(t, target, oldB)
	oldFI, _ := os.Stat(target)

	reader, _ := os.Open(target)
	p := makePatch(t, oldB, newB, 256)
	res, err := Apply(p, reader, target, Options{Space: fakeSpace(1 << 30)})
	if err != nil {
		reader.Close()
		t.Fatal(err)
	}
	// The read handle must still see the ORIGINAL bytes even after rename —
	// because rename replaced the directory entry, our open fd points at the
	// unlinked old inode.
	buf := make([]byte, len(oldB))
	if _, err := reader.ReadAt(buf, 0); err != nil {
		reader.Close()
		t.Fatal(err)
	}
	reader.Close()
	if !bytes.Equal(buf, oldB) {
		t.Fatal("old read handle did not observe stable old content")
	}
	newFI, _ := os.Stat(target)
	if os.SameFile(oldFI, newFI) {
		t.Fatal("expected inode to change (atomic rename), got same inode (in-place write)")
	}
	_ = res
}

// TestApplyIdempotentAfterRename: if target is already at NewSum, apply
// reports already-applied and leaves it alone.
func TestApplyIdempotentAfterRename(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("O"), 4096)
	newB := bytes.Repeat([]byte("N"), 4096)
	writeTarget(t, target, newB) // already new
	p := makePatch(t, oldB, newB, 256)
	f, _ := os.Open(target)
	res, err := Apply(p, f, target, Options{Space: fakeSpace(1 << 30)})
	f.Close()
	if !AlreadyApplied(err) {
		t.Fatalf("want ErrAlreadyApplied, got %v", err)
	}
	if res.Applied {
		t.Fatal("expected Applied=false")
	}
}

// TestApplyCreateFromEmpty: target does not exist, oldSize==0.
func TestApplyCreateFromEmpty(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "new.bin")
	newB := []byte("brand new artifact content")
	p := makePatch(t, nil, newB, 16)
	res, err := Apply(p, nil, target, Options{Space: fakeSpace(1 << 30)})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Applied {
		t.Fatal("expected apply")
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, newB) {
		t.Fatal("content mismatch")
	}
}

// TestApplyMissingBaseExpected: target missing but patch expects a base.
func TestApplyMissingBaseExpected(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "x.bin")
	oldB := []byte("there should be a base")
	newB := []byte("new contents here!!!")
	p := makePatch(t, oldB, newB, 16)
	_, err := Apply(p, nil, target, Options{Space: fakeSpace(1 << 30)})
	if !errors.Is(err, ErrWrongBase) {
		t.Fatalf("want wrong base, got %v", err)
	}
}
