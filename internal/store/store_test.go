package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	st, err := openTestWithDir(context.Background(), filepath.Join(dir, "cache.db"), dir)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

// openTestWithDir mirrors Open but accepts explicit paths (Open already does,
// this is just a short alias).
func openTestWithDir(ctx context.Context, dbPath, dataDir string) (*Store, error) {
	return Open(ctx, dbPath, dataDir)
}

func TestClaimSinglePublisher(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	k := "bck1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	first, e, err := st.Claim(ctx, k)
	if err != nil || !first || e == nil || e.Status != StatusBuilding {
		t.Fatalf("first claim = %v,%v,%v want true,building", first, e, err)
	}
	second, e2, err := st.Claim(ctx, k)
	if err != nil {
		t.Fatal(err)
	}
	if second || e2.Status != StatusBuilding {
		t.Fatalf("second concurrent claim = %v,%v want false,building", second, e2)
	}

	// First publisher publishes success.
	e.Status = StatusSucceeded
	e.ArtifactPath = "aa/bb"
	e.ArtifactSize = 5
	e.ArtifactSHA = "x"
	_ = e
	// Proper 64-hex sha required only at the builder layer; store accepts any.
	e.ArtifactSHA = "1111111111111111111111111111111111111111111111111111111111111111"
	if err := st.PublishResult(ctx, e); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// Now the next claim rebuilds (row left building again, attempt bumps).
	third, e3, err := st.Claim(ctx, k)
	if err != nil || !third {
		t.Fatalf("claim after success = %v,%v want true", third, err)
	}
	if e3.Attempt != 2 {
		t.Fatalf("attempt = %d want 2", e3.Attempt)
	}
	if e3.ArtifactSHA != "" || e3.ArtifactPath != "" {
		t.Fatalf("rebuild claim must clear artifact fields: %+v", e3)
	}
}

func TestPublishFailureNeverCarriesArtifact(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	k := "bck1-" + string(repeat('2', 64))
	_, e, _ := st.Claim(ctx, k)
	e.Status = StatusFailed
	e.ExitCode = 2
	e.ArtifactPath = "should/be/dropped"
	e.ArtifactSHA = "3333333333333333333333333333333333333333333333333333333333333333"
	e.ErrMessage = "compile error"
	if err := st.PublishResult(ctx, e); err != nil {
		t.Fatalf("publish failure: %v", err)
	}
	got, err := st.Get(ctx, k)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != StatusFailed {
		t.Fatalf("status=%s want failed", got.Status)
	}
	if got.ArtifactPath != "" || got.ArtifactSHA != "" || got.ArtifactSize != 0 {
		t.Fatalf("failure row leaked artifact fields: %+v", got)
	}
	if got.ExitCode != 2 {
		t.Fatalf("exit code = %d want 2", got.ExitCode)
	}
}

func TestPublishRejectsBadStatus(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	k := "bck1-" + string(repeat('4', 64))
	_, e, _ := st.Claim(ctx, k)
	e.Status = StatusSucceeded
	// Missing required artifact fields.
	if err := st.PublishResult(ctx, e); err == nil {
		t.Fatal("expected error publishing success without artifact")
	}
}

func TestRestartRecoversStaleLeases(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cache.db")
	ctx := context.Background()
	st, err := Open(ctx, dbPath, dir)
	if err != nil {
		t.Fatal(err)
	}
	keys := []string{
		"bck1-" + string(repeat('a', 64)),
		"bck1-" + string(repeat('b', 64)),
	}
	for _, k := range keys {
		if _, _, err := st.Claim(ctx, k); err != nil {
			t.Fatal(err)
		}
	}
	// Simulate a hard crash: close without publishing.
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// New process boots.
	st2, err := Open(context.Background(), dbPath, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	n, err := st2.ResetStaleLeases(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("recovered %d rows want 2", n)
	}
	for _, k := range keys {
		e, err := st2.Get(context.Background(), k)
		if err != nil {
			t.Fatal(err)
		}
		if e.Status != StatusInterrupted {
			t.Fatalf("status=%s want interrupted", e.Status)
		}
	}
	// Interrupted rows are claimable again by a new publisher.
	claimed, e, err := st2.Claim(context.Background(), keys[0])
	if err != nil || !claimed {
		t.Fatalf("claim after recovery = %v,%v want true", claimed, err)
	}
	if e.Attempt != 2 {
		t.Fatalf("attempt=%d want 2", e.Attempt)
	}
	// Finish that rebuild cleanly so the process exits with no lease held.
	e.Status = StatusSucceeded
	e.ArtifactPath = "aa/bb"
	e.ArtifactSize = 5
	e.ArtifactSHA = string(repeat('e', 64))
	if err := st2.PublishResult(context.Background(), e); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// Second boot finds nothing stale (idempotent).
	if err := st2.Close(); err != nil {
		t.Fatal(err)
	}
	st3, _ := Open(context.Background(), dbPath, dir)
	defer st3.Close()
	n, _ = st3.ResetStaleLeases(context.Background())
	if n != 0 {
		t.Fatalf("second recovery moved %d rows want 0", n)
	}
}

func TestQuarantine(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	k := "bck1-" + string(repeat('c', 64))
	_, e, _ := st.Claim(ctx, k)
	e.Status = StatusSucceeded
	e.ArtifactPath = "cc/dd"
	e.ArtifactSize = 9
	e.ArtifactSHA = string(repeat('c', 64))
	if err := st.PublishResult(ctx, e); err != nil {
		t.Fatal(err)
	}
	q, err := st.Quarantine(ctx, k, "hash_mismatch", "tampered",
		string(repeat('c', 64)), string(repeat('f', 64)))
	if err != nil {
		t.Fatal(err)
	}
	if q.ID <= 0 {
		t.Fatal("event id not set")
	}
	got, _ := st.Get(ctx, k)
	if got.Status != StatusQuarantined || got.ArtifactSHA != "" {
		t.Fatalf("quarantined row wrong: %+v", got)
	}
	evs, err := st.QuarantineEvents(ctx, k)
	if err != nil || len(evs) != 1 || evs[0].Reason != "hash_mismatch" {
		t.Fatalf("events = %v,%v", evs, err)
	}
}

func TestGetMissing(t *testing.T) {
	st := openTestStore(t)
	if _, err := st.Get(context.Background(), "bck1-"+string(repeat('0', 64))); err != ErrNotFound {
		t.Fatalf("err=%v want ErrNotFound", err)
	}
}

func TestClaimSerializesContention(t *testing.T) {
	// With MaxOpenConns(1) and busy_timeout, hammering Claim must never error
	// and only one publisher may ever win while a lease is held.
	st := openTestStore(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	k := "bck1-" + string(repeat('d', 64))
	winners := make(chan bool, 50)
	for i := 0; i < 50; i++ {
		go func() {
			ok, _, err := st.Claim(ctx, k)
			if err != nil {
				t.Errorf("claim error: %v", err)
				return
			}
			winners <- ok
		}()
	}
	wins := 0
	for i := 0; i < 50; i++ {
		if <-winners {
			wins++
		}
	}
	if wins != 1 {
		t.Fatalf("publishers=%d want exactly 1", wins)
	}
}

func repeat(b byte, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}
