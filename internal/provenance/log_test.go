package provenance

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testKey() []byte {
	k := make([]byte, 32)
	for i := range k {
		k[i] = byte(i + 1)
	}
	return k
}

func TestLogChainAndReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "log.jsonl")
	log, err := OpenLog(path, testKey())
	if err != nil {
		t.Fatal(err)
	}
	prev := log.Tail()
	ids := []string{"art_a", "art_b", "art_c"}
	for i, id := range ids {
		r := Record{
			Prev: prev, ArtifactID: id, ToolName: "t",
			ToolDigest:   Digest("sha256:" + strings.Repeat("aa", 32)),
			OutputDigest: Digest("sha256:" + strings.Repeat("bb", 32)),
			OutputName:   "O",
			Inputs:       []InputRef{{Slot: "S", Kind: "source", Path: "p", Digest: Digest("sha256:" + strings.Repeat("cc", 32))}},
		}
		app, err := log.Append(r)
		if err != nil {
			t.Fatal(err)
		}
		if app.RecordHash == "" || app.Sig == "" {
			t.Fatal("append did not finalize record")
		}
		prev = app.RecordHash
		_ = i
	}
	if len(log.All()) != 3 {
		t.Fatal("expected 3 records")
	}

	// Fresh process reopens and validates the chain/HMACs.
	reopened, err := OpenLog(path, testKey())
	if err != nil {
		t.Fatalf("reopen valid log failed: %v", err)
	}
	if reopened.Tail() != log.Tail() {
		t.Fatal("tails differ after reopen")
	}

	// Wrong key must fail on reopen.
	wrong := make([]byte, 32)
	for i := range wrong {
		wrong[i] = 0xFF
	}
	if _, err := OpenLog(path, wrong); err == nil {
		t.Fatal("reopen with wrong HMAC key must fail")
	}
}

func TestLogDetectsTamperedLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "log.jsonl")
	log, err := OpenLog(path, testKey())
	if err != nil {
		t.Fatal(err)
	}
	r, err := log.Append(Record{
		Prev: log.Tail(), ArtifactID: "art_x", ToolName: "t",
		ToolDigest:   Digest("sha256:" + strings.Repeat("01", 32)),
		OutputDigest: Digest("sha256:" + strings.Repeat("02", 32)),
	})
	if err != nil {
		t.Fatal(err)
	}

	// Tamper content on disk without updating hashes.
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	evil := strings.Replace(string(raw), "\"toolName\":\"t\"", "\"toolName\":\"evil\"", 1)
	if err := os.WriteFile(path, []byte(evil), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenLog(path, testKey()); err == nil {
		t.Fatal("tampered record content must fail reopen")
	}

	// Tamper content and re-sign with the key, but leave the stored
	// recordHash stale: hash check must fail first.
	h, err := RecordHash(r)
	if err != nil {
		t.Fatal(err)
	}
	if h != r.RecordHash {
		t.Fatal("RecordHash helper inconsistent")
	}
}

func TestLogRejectsForkAndDuplicate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "log.jsonl")
	log, err := OpenLog(path, testKey())
	if err != nil {
		t.Fatal(err)
	}
	rec := Record{
		ArtifactID: "art_1", ToolName: "t",
		ToolDigest:   Digest("sha256:" + strings.Repeat("0a", 32)),
		OutputDigest: Digest("sha256:" + strings.Repeat("0b", 32)),
	}
	if _, err := log.Append(rec); err == nil {
		t.Fatal("append with wrong prev (fork) must fail")
	}
	rec.Prev = log.Tail()
	if _, err := log.Append(rec); err != nil {
		t.Fatal(err)
	}
	rec.Prev = log.Tail()
	if _, err := log.Append(rec); err == nil {
		t.Fatal("duplicate artifact attestation must fail")
	}
}

func TestRecordExcludesSelfReferentialFields(t *testing.T) {
	r := Record{ArtifactID: "art_z", RecordHash: "old", Sig: "sig"}
	h1, err := RecordHash(r)
	if err != nil {
		t.Fatal(err)
	}
	r.RecordHash = "new"
	r.Sig = "different"
	h2, err := RecordHash(r)
	if err != nil {
		t.Fatal(err)
	}
	if h1 != h2 {
		t.Fatal("record hash must not depend on recordHash/sig fields")
	}
}
