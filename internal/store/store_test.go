package store

import (
	"path/filepath"
	"testing"
	"time"
)

func TestSinglePublisher(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "m.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	ok, err := s.TryAcquire("k1", "owner-a", "{}", "{}", time.Minute)
	if err != nil || !ok {
		t.Fatalf("first acquire: ok=%v err=%v", ok, err)
	}
	ok, err = s.TryAcquire("k1", "owner-b", "{}", "{}", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("second publisher must not acquire a held key")
	}
}

func TestLeaseExpiryAllowsTakeover(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "m.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	ok, err := s.TryAcquire("k1", "owner-a", "{}", "{}", 50*time.Millisecond)
	if err != nil || !ok {
		t.Fatal("first acquire failed")
	}
	time.Sleep(80 * time.Millisecond)
	ok, err = s.TryAcquire("k1", "owner-b", "{}", "{}", time.Minute)
	if err != nil || !ok {
		t.Fatalf("expired lease must be stealable: ok=%v err=%v", ok, err)
	}
}

func TestFailedBuildNeverServedAsSuccess(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "m.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	s.TryAcquire("k1", "a", "{}", "{}", time.Minute)
	if err := s.PublishFailure("k1", "a", "compile error"); err != nil {
		t.Fatal(err)
	}
	ent, err := s.Get("k1")
	if err != nil {
		t.Fatal(err)
	}
	if ent.Status != StatusFailed || ent.Error != "compile error" {
		t.Fatalf("got %+v", ent)
	}
	// A failed entry can be re-acquired for a real rebuild.
	ok, _ := s.TryAcquire("k1", "b", "{}", "{}", time.Minute)
	if !ok {
		t.Fatal("failed entry must be re-acquirable")
	}
	if err := s.PublishSuccess("k1", "b", "deadbeef", 4, "ok"); err != nil {
		t.Fatal(err)
	}
	ent, _ = s.Get("k1")
	if ent.Status != StatusSuccess || ent.ArtifactDigest != "deadbeef" {
		t.Fatalf("got %+v", ent)
	}
}

func TestRestartRecoversInterruptedBuilds(t *testing.T) {
	db := filepath.Join(t.TempDir(), "m.db")
	s, err := Open(db)
	if err != nil {
		t.Fatal(err)
	}
	if ok, _ := s.TryAcquire("k1", "a", "{}", "{}", time.Hour); !ok {
		t.Fatal("acquire failed")
	}
	s.Close() // simulate crash: lease left in "building"

	s2, err := Open(db)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	ent, err := s2.Get("k1")
	if err != nil {
		t.Fatal(err)
	}
	if ent.Status != StatusFailed {
		t.Fatalf("interrupted build must be marked failed after restart, got %s", ent.Status)
	}
}
