package evidence_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"forensiccore/internal/cases"
	"forensiccore/internal/domain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/securefile"
	"forensiccore/internal/testkit"
)

const chunkSize = 64 * 1024

func sha256Hex(t *testing.T, b []byte) string {
	t.Helper()
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func makeImage(n int, fill byte) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = fill
	}
	return b
}

func TestRegisterBaseline(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	caseID := env.CreateCase(t, "case-a")

	data := makeImage(200*1024, 0x5A)
	path := env.WriteFile(t, "disk_a.dd", data)

	ev, chainEv, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if ev.Size != int64(len(data)) {
		t.Fatalf("size = %d, want %d", ev.Size, len(data))
	}
	if ev.SHA256 != sha256Hex(t, data) {
		t.Fatalf("baseline sha256 mismatch")
	}
	if chainEv.Seq != 2 { // genesis case_created is seq 1
		t.Fatalf("registration chain seq = %d, want 2", chainEv.Seq)
	}
	if ev.RegisteredSeq != 2 {
		t.Fatalf("registered_seq = %d, want 2", ev.RegisteredSeq)
	}

	// Re-registering the same resolved path is rejected.
	if _, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	}); !errors.Is(err, evidence.ErrAlreadyExists) {
		t.Fatalf("expected ErrAlreadyExists, got %v", err)
	}
}

// TestRegisterFailsWhenFileChangesDuringRead installs a hook that mutates the
// image during the first hashing pass and asserts that no evidence baseline is
// persisted.
func TestRegisterFailsWhenFileChangesDuringRead(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	caseID := env.CreateCase(t, "case-changing")

	data := makeImage(500*1024, 0x11)
	path := env.WriteFile(t, "changing.dd", data)

	var once sync.Once
	env.Evidence.RegisterHook = func(index int, _, _ int64) {
		// Trigger mid-read (several chunks in) so the mutation is strictly
		// after the open and detected by identity comparison.
		if index == 2 {
			once.Do(func() {
				time.Sleep(2 * time.Millisecond)
				tweaked := makeImage(500*1024, 0x22)
				if err := os.WriteFile(path, tweaked, 0o644); err != nil {
					t.Errorf("mutate: %v", err)
				}
			})
		}
	}

	_, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	})
	if !errors.Is(err, securefile.ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged, got %v", err)
	}

	// No evidence row, and no registered chain event beyond the genesis event.
	list, err := env.Evidence.ListByCase(context.Background(), caseID)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 0 {
		t.Fatalf("no baseline may be saved from a moving file, got %d rows", len(list))
	}
	evs, err := env.Chain.ListEvents(context.Background(), caseID)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range evs {
		if e.EventType == domain.EventRegistered {
			t.Fatal("a registered chain event was persisted despite failed hashing")
		}
	}
}

func TestRegisterRejectsPathEscape(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	caseID := env.CreateCase(t, "case-escape")
	outside := env.WriteOutside(t, "secret.dd", makeImage(1024, 0x33))

	_, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID:     caseID,
		SourcePath: filepath.Join(env.Whitelist, "..", "outside", "secret.dd"),
		Actor:      "role:investigator",
	})
	if !errors.Is(err, securefile.ErrOutsideWhitelist) {
		t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
	}

	// Absolute path to the outside file must also fail.
	_, _, err = env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: outside, Actor: "role:investigator",
	})
	if !errors.Is(err, securefile.ErrOutsideWhitelist) {
		t.Fatalf("expected ErrOutsideWhitelist, got %v", err)
	}
}

func TestRegisterRejectsNonRaw(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	caseID := env.CreateCase(t, "case-ext")
	path := env.WriteFile(t, "image.img", []byte("raw bytes but wrong ext"))
	_, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	})
	if !errors.Is(err, securefile.ErrUnsupportedType) {
		t.Fatalf("expected ErrUnsupportedType, got %v", err)
	}
}

func TestUnknownCaseRegister(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	path := env.WriteFile(t, "x.dd", makeImage(128, 1))
	_, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: "missing", SourcePath: path, Actor: "role:investigator",
	})
	if !errors.Is(err, evidence.ErrCaseNotFound) && !errors.Is(err, cases.ErrNotFound) {
		t.Fatalf("expected case-not-found, got %v", err)
	}
}

func TestTransferAndNotes(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	caseID := env.CreateCase(t, "custody")
	path := env.WriteFile(t, "disk.dd", makeImage(1024, 9))
	ev, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := env.Evidence.Transfer(context.Background(), evidence.TransferInput{
		CaseID: caseID, EvidenceID: ev.ID,
		ToCustodian: "lab-7",
		Reason:      "handed to analyst lab", Actor: "role:investigator",
	}); err != nil {
		t.Fatalf("transfer: %v", err)
	}

	got, err := env.Evidence.Get(context.Background(), ev.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Custodian != "lab-7" {
		t.Fatalf("custodian = %q, want lab-7", got.Custodian)
	}

	if _, err := env.Evidence.AddNote(context.Background(), evidence.NoteInput{
		CaseID: caseID, EvidenceID: ev.ID, Note: "chain of custody intact",
		Actor: "role:analyst",
	}); err != nil {
		t.Fatalf("note: %v", err)
	}

	rep, err := env.Chain.Verify(context.Background(), caseID)
	if err != nil || !rep.Intact {
		t.Fatalf("chain verify intact=%v err=%v issues=%v", rep.Intact, err, rep.Issues)
	}
	if !strings.Contains(string(mustChainJSON(t, env, caseID)), "lab-7") {
		t.Fatal("transfer payload missing from chain")
	}
}

func mustChainJSON(t *testing.T, env *testkit.Env, caseID string) []byte {
	t.Helper()
	evs, err := env.Chain.ListEvents(context.Background(), caseID)
	if err != nil {
		t.Fatal(err)
	}
	var sb strings.Builder
	for _, e := range evs {
		sb.WriteString(e.ContentJSON)
	}
	return []byte(sb.String())
}
