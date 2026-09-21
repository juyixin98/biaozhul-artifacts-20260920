package service_test

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"forensiccore/internal/models"
	"forensiccore/internal/safeio"
	"forensiccore/internal/service"
	"forensiccore/internal/testutil"
)

func writeEvidence(t *testing.T, env *testutil.Env, name string, data []byte) string {
	t.Helper()
	p := filepath.Join(env.EvidenceRoot, name)
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestRegisterRecordsBaselineAndGenesisEvent(t *testing.T) {
	env := testutil.New(t, 16, nil)
	data := []byte("evidence baseline contents for registration test")
	writeEvidence(t, env, "disk01.raw", data)

	c, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "CASE-001", File: "disk01.raw"}, "alice")
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	sum := sha256.Sum256(data)
	if c.SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("baseline %s != %s", c.SHA256, hex.EncodeToString(sum[:]))
	}
	if c.Size != int64(len(data)) {
		t.Fatalf("size = %d", c.Size)
	}
	if c.RegisteredBy != "alice" {
		t.Fatalf("registered_by = %s", c.RegisteredBy)
	}

	events, err := env.Service.ListEvents(c.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].Type != models.EventRegister || events[0].Seq != 1 {
		t.Fatalf("unexpected genesis events: %+v", events)
	}
	res, err := env.Service.VerifyChain(c.ID)
	if err != nil || !res.OK {
		t.Fatalf("chain verify: %v %+v", err, res.Faults)
	}
}

func TestRegisterRejectsNonRawDdExtensions(t *testing.T) {
	env := testutil.New(t, 16, nil)
	writeEvidence(t, env, "notes.txt", []byte("hello"))
	writeEvidence(t, env, "image.E01", []byte("hello"))
	writeEvidence(t, env, "noext", []byte("hello"))

	for _, name := range []string{"notes.txt", "image.E01", "noext"} {
		_, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "X-" + name, File: name}, "alice")
		if err == nil {
			t.Fatalf("%s should be rejected", name)
		}
	}
	var n int64
	env.DB.Model(&models.Case{}).Count(&n)
	if n != 0 {
		t.Fatalf("no cases should be persisted, found %d", n)
	}
}

func TestRegisterPathTraversalRejected(t *testing.T) {
	env := testutil.New(t, 16, nil)
	outside := filepath.Join(t.TempDir(), "evil.raw")
	if err := os.WriteFile(outside, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "EVIL", File: "../../evil.raw"}, "alice")
	if err == nil {
		t.Fatal("traversal must be rejected")
	}
	if !errors.Is(err, safeio.ErrOutsideWhitelist) && !errors.Is(err, safeio.ErrIllegalName) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestRegisterChangeDuringReadSavesNoBaseline(t *testing.T) {
	env := testutil.New(t, 16, nil)
	path := writeEvidence(t, env, "mutating.raw", make([]byte, 128))

	var fired bool
	env.Service.RegisterHook = func(off int64) error {
		if !fired && off == 32 {
			fired = true
			w, err := os.OpenFile(path, os.O_WRONLY, 0)
			if err != nil {
				return err
			}
			defer w.Close()
			if _, err := w.WriteAt([]byte{0x01, 0x02, 0x03}, 0); err != nil {
				return err
			}
			return w.Sync()
		}
		return nil
	}

	_, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "MUT", File: "mutating.raw"}, "alice")
	if !errors.Is(err, safeio.ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged, got %v", err)
	}

	// The wrong baseline must never be persisted.
	var n int64
	env.DB.Model(&models.Case{}).Count(&n)
	if n != 0 {
		t.Fatalf("no case row should exist after failure, found %d", n)
	}
	env.DB.Model(&models.ChainEvent{}).Count(&n)
	if n != 0 {
		t.Fatalf("no chain events should exist after failure, found %d", n)
	}
}

func TestRegisterDuplicateCaseRefOrFileRejected(t *testing.T) {
	env := testutil.New(t, 16, nil)
	writeEvidence(t, env, "a.raw", []byte("aaaa"))
	writeEvidence(t, env, "b.raw", []byte("bbbb"))
	if _, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "DUP", File: "a.raw"}, "alice"); err != nil {
		t.Fatal(err)
	}
	if _, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "DUP", File: "b.raw"}, "alice"); !errors.Is(err, service.ErrConflict) {
		t.Fatalf("duplicate case_ref: expected conflict, got %v", err)
	}
	if _, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "OTHER", File: "a.raw"}, "alice"); !errors.Is(err, service.ErrConflict) {
		t.Fatalf("duplicate file: expected conflict, got %v", err)
	}
}

func TestSourceImageNeverModifiedByService(t *testing.T) {
	env := testutil.New(t, 16, nil)
	data := make([]byte, 64)
	for i := range data {
		data[i] = byte(i)
	}
	path := writeEvidence(t, env, "immutable.raw", data)
	infoBefore, _ := os.Stat(path)
	c, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: "IMMUT", File: "immutable.raw"}, "alice")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := env.Service.Note(c.ID, "remark", "alice"); err != nil {
		t.Fatal(err)
	}
	if _, err := env.Service.Transfer(c.ID, service.TransferRequest{ToCustodian: "lab"}, "alice"); err != nil {
		t.Fatal(err)
	}
	infoAfter, _ := os.Stat(path)
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(got) != hex.EncodeToString(data) {
		t.Fatal("evidence image bytes changed")
	}
	if !infoAfter.ModTime().Equal(infoBefore.ModTime()) || infoAfter.Size() != infoBefore.Size() {
		t.Fatal("evidence image metadata changed")
	}
}
