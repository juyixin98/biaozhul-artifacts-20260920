package dataset_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"sync"
	"testing"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/dataset"
	"synapticgo/internal/models"
	"synapticgo/internal/testutil"
)

type fixture struct {
	t    *testing.T
	env  *testutil.Env
	svc  *dataset.Service
	data []byte
	cs   int
}

func newFixture(t *testing.T, ownerID int64, data []byte, cs int) *fixture {
	t.Helper()
	env := testutil.New(t)
	svc := dataset.NewService(env.DB, env.Objects, env.Store)
	return &fixture{t: t, env: env, svc: svc, data: data, cs: cs}
}

func digest(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func (f *fixture) chunk(i int) []byte {
	start := i * f.cs
	end := start + f.cs
	if end > len(f.data) {
		end = len(f.data)
	}
	return f.data[start:end]
}

// uploadAll uploads every chunk in the given order.
func (f *fixture) uploadAll(ctx context.Context, owner, id int64, order []int) {
	f.t.Helper()
	for _, i := range order {
		b := f.chunk(i)
		_, _, err := f.svc.UploadChunk(ctx, owner, id, int32(i), bytes.NewReader(b), digest(b))
		if err != nil {
			f.t.Fatalf("upload chunk %d: %v", i, err)
		}
	}
}

func createUser(t *testing.T, db *sqlx.DB, name string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRowx(
		`INSERT INTO users(username, key_hash) VALUES ($1, $2) RETURNING id`,
		name, "hash:"+name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestOutOfOrderUploadAndResume(t *testing.T) {
	ctx := context.Background()
	data := []byte("the quick brown fox jumps over the lazy dog, 0123456789!")
	owner := int64(1)
	f := newFixture(t, owner, data, 10)
	_ = createUser(t, f.env.DB, "alice")

	d, err := f.svc.Create(ctx, owner, "d", int64(len(data)), 10, nil)
	if err != nil {
		t.Fatal(err)
	}

	// Deliberately out of order; upload every chunk except the last (5).
	order := []int{3, 0, 2, 1, 4}
	for _, i := range order {
		b := f.chunk(i)
		_, idem, err := f.svc.UploadChunk(ctx, owner, d.ID, int32(i), bytes.NewReader(b), digest(b))
		if err != nil {
			t.Fatalf("chunk %d: %v", i, err)
		}
		if idem {
			t.Fatalf("first send of chunk %d must not be idempotent", i)
		}
	}
	// Another send of chunk 3 must be flagged idempotent.
	b3 := f.chunk(3)
	_, idem, err := f.svc.UploadChunk(ctx, owner, d.ID, 3, bytes.NewReader(b3), digest(b3))
	if err != nil || !idem {
		t.Fatalf("retry idempotent=%v err=%v", idem, err)
	}

	// Missing last chunk (5): publish must fail.
	if _, _, err := f.svc.Publish(ctx, owner, d.ID); err == nil {
		t.Fatal("expected missing-chunk error")
	}
	// Resume: upload the missing chunk and publish succeeds.
	b5 := f.chunk(5)
	if _, _, err := f.svc.UploadChunk(ctx, owner, d.ID, 5, bytes.NewReader(b5), digest(b5)); err != nil {
		t.Fatal(err)
	}
	got, whole, err := f.svc.Publish(ctx, owner, d.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != models.StatusReady {
		t.Fatalf("status = %s", got.Status)
	}
	if whole != digest(data) {
		t.Fatalf("whole digest mismatch")
	}

	// Content round-trips exactly.
	_, _, rc, err := f.svc.OpenContent(ctx, owner, d.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	out, _ := io.ReadAll(rc)
	if !bytes.Equal(out, data) {
		t.Fatalf("content mismatch: %q", out)
	}
}

func TestContentConflictRejected(t *testing.T) {
	ctx := context.Background()
	data := bytes.Repeat([]byte{0xAB}, 25)
	owner := int64(1)
	f := newFixture(t, owner, data, 10)
	_ = createUser(t, f.env.DB, "alice")
	d, _ := f.svc.Create(ctx, owner, "d", int64(len(data)), 10, nil)

	// Wrong declared digest -> reject (server hashes the body itself).
	_, _, err := f.svc.UploadChunk(ctx, owner, d.ID, 0, bytes.NewReader(f.chunk(0)),
		"0000000000000000000000000000000000000000000000000000000000000000")
	if err == nil {
		t.Fatal("expected digest mismatch")
	}

	// Correct upload first.
	b0 := f.chunk(0)
	if _, _, err := f.svc.UploadChunk(ctx, owner, d.ID, 0, bytes.NewReader(b0), digest(b0)); err != nil {
		t.Fatal(err)
	}
	// Same index, different content -> conflict.
	other := bytes.Repeat([]byte{0xCD}, 10)
	if _, _, err := f.svc.UploadChunk(ctx, owner, d.ID, 0, bytes.NewReader(other), digest(other)); err == nil {
		t.Fatal("expected content conflict on existing slot")
	}
}

func TestWholeDigestMismatchBlocksPublish(t *testing.T) {
	ctx := context.Background()
	data := []byte("hello world this is dataset content")
	owner := int64(1)
	f := newFixture(t, owner, data, 8)
	_ = createUser(t, f.env.DB, "alice")
	wrong := "1111111111111111111111111111111111111111111111111111111111111111"
	d, err := f.svc.Create(ctx, owner, "d", int64(len(data)), 8, &wrong)
	if err != nil {
		t.Fatal(err)
	}
	n := (len(data) + 7) / 8
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	f.uploadAll(ctx, owner, d.ID, order)
	if _, _, err := f.svc.Publish(ctx, owner, d.ID); err == nil {
		t.Fatal("expected whole-digest mismatch to block publish")
	}
	// Dataset stays uploading and can be retried (the blob assembly here is
	// deterministic, so a corrected declaration path is exercised separately:
	// status must not be "ready").
	got, _ := f.svc.Get(ctx, owner, d.ID)
	if got.Status == models.StatusReady {
		t.Fatal("half file must never be marked ready")
	}
}

// simulateCrash runs fn and asserts it fails like a killed process.
func simulateCrash(t *testing.T, fn func() error) {
	t.Helper()
	if err := fn(); err == nil {
		t.Fatal("expected injected crash error")
	}
}

func TestPublishCrashBeforeCommitIsRecoverable(t *testing.T) {
	ctx := context.Background()
	data := bytes.Repeat([]byte("recover-me-"), 4) // 40 bytes
	owner := int64(1)
	f := newFixture(t, owner, data, 12)
	_ = createUser(t, f.env.DB, "alice")
	d, _ := f.svc.Create(ctx, owner, "d", int64(len(data)), 12, nil)
	n := (len(data) + 11) / 12
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	f.uploadAll(ctx, owner, d.ID, order)

	// Inject a failure after assembly, before the metadata commit.
	f.svc.Faults.AfterAssemble = func(id int64) error {
		return fmt.Errorf("simulated SIGKILL")
	}
	simulateCrash(t, func() error {
		_, _, err := f.svc.Publish(ctx, owner, d.ID)
		return err
	})
	f.svc.Faults.AfterAssemble = nil

	// Simulate process restart: staging leftovers are purged, and the dataset
	// is still uploading (no half-file marked ready). Republish succeeds.
	f.env.Restart()
	svc2 := dataset.NewService(f.env.DB, f.env.Objects, f.env.Store)
	got, _ := svc2.Get(ctx, owner, d.ID)
	if got.Status != models.StatusUploading {
		t.Fatalf("status after crash = %s, want uploading", got.Status)
	}
	pub, whole, err := svc2.Publish(ctx, owner, d.ID)
	if err != nil {
		t.Fatalf("republish after recovery: %v", err)
	}
	if pub.Status != models.StatusReady || whole != digest(data) {
		t.Fatal("recovered publish wrong")
	}
}

func TestOrphanBlobAfterRenameCrashIsCleaned(t *testing.T) {
	ctx := context.Background()
	data := []byte("orphan blob cleanup payload here!!!")
	owner := int64(1)
	f := newFixture(t, owner, data, 11)
	_ = createUser(t, f.env.DB, "alice")
	d, _ := f.svc.Create(ctx, owner, "d", int64(len(data)), 11, nil)
	n := (len(data) + 10) / 11
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	f.uploadAll(ctx, owner, d.ID, order)

	// Crash on the whole-object rename window.
	f.env.Objects.FaultAfterCommitStaging = func(string) error {
		return fmt.Errorf("simulated crash between rename and insert")
	}
	if _, _, err := f.svc.Publish(ctx, owner, d.ID); err == nil {
		t.Fatal("expected crash")
	}
	f.env.Objects.FaultAfterCommitStaging = nil

	// Before restart there is an orphan file with no row. Restart must remove
	// it without damaging referenced objects.
	f.env.Restart()

	// Every object on disk must have a row (recovery invariant).
	known := func(dgst string) (bool, error) {
		var ok bool
		err := f.env.DB.Get(&ok, `SELECT EXISTS(SELECT 1 FROM objects WHERE digest=$1)`, dgst)
		return ok, err
	}
	orphans, err := f.env.Store.OrphanObjects(known)
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("orphans remain after recovery: %v", orphans)
	}
}

func TestConcurrentPublishOnlyOneSucceeds(t *testing.T) {
	ctx := context.Background()
	data := bytes.Repeat([]byte("race!"), 8)
	owner := int64(1)
	f := newFixture(t, owner, data, 7)
	_ = createUser(t, f.env.DB, "alice")
	d, _ := f.svc.Create(ctx, owner, "d", int64(len(data)), 7, nil)
	n := (len(data) + 6) / 7
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	f.uploadAll(ctx, owner, d.ID, order)

	const workers = 8
	var wg sync.WaitGroup
	var okN, failN int64
	var mu sync.Mutex
	start := make(chan struct{})
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, _, err := f.svc.Publish(ctx, owner, d.ID)
			mu.Lock()
			if err == nil {
				okN++
			} else {
				failN++
			}
			mu.Unlock()
		}()
	}
	close(start)
	wg.Wait()
	if okN != 1 || failN != workers-1 {
		t.Fatalf("publish race: ok=%d fail=%d", okN, failN)
	}
	// Whole object exists exactly once with one ref.
	var rc int
	var dgst string
	if err := f.env.DB.QueryRowx(`
		SELECT o.refcount, o.digest FROM objects o
		JOIN datasets d ON d.whole_object_id = o.id WHERE d.id=$1`, d.ID).Scan(&rc, &dgst); err != nil {
		t.Fatal(err)
	}
	if rc != 1 {
		t.Fatalf("whole object refcount = %d, want 1", rc)
	}
	if dgst != digest(data) {
		t.Fatal("wrong whole digest after racing publish")
	}
}

func TestDuplicateContentSharesObjectAndIsolation(t *testing.T) {
	ctx := context.Background()
	data := []byte("identical payload shared between two owners, padded 12345")
	cs := 13
	env := testutil.New(t)
	_ = createUser(t, env.DB, "a")
	_ = createUser(t, env.DB, "b")
	svc := dataset.NewService(env.DB, env.Objects, env.Store)

	mk := func(owner int64) int64 {
		d, err := svc.Create(ctx, owner, "x", int64(len(data)), int32(cs), nil)
		if err != nil {
			t.Fatal(err)
		}
		n := (len(data) + cs - 1) / cs
		for i := 0; i < n; i++ {
			chunk := data[i*cs:]
			if len(chunk) > cs {
				chunk = chunk[:cs]
			}
			if _, _, err := svc.UploadChunk(ctx, owner, d.ID, int32(i), bytes.NewReader(chunk), digest(chunk)); err != nil {
				t.Fatal(err)
			}
		}
		pub, _, err := svc.Publish(ctx, owner, d.ID)
		if err != nil {
			t.Fatal(err)
		}
		return pub.ID
	}
	da := mk(1)
	dbID := mk(2)

	// One object row, shared, refcount 2.
	var rows int
	if err := env.DB.Get(&rows, `SELECT count(*) FROM objects WHERE digest=$1`, digest(data)); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("expected a single shared object row, got %d", rows)
	}
	var rc int
	if err := env.DB.Get(&rc, `SELECT refcount FROM objects WHERE digest=$1`, digest(data)); err != nil {
		t.Fatal(err)
	}
	if rc != 2 {
		t.Fatalf("shared refcount = %d want 2", rc)
	}

	// Owner isolation: owner 1 cannot read or delete owner 2's dataset.
	if _, _, _, err := svc.OpenContent(ctx, 1, dbID); err == nil {
		t.Fatal("cross-owner content access must fail")
	}
	if err := svc.Delete(ctx, 1, dbID); err == nil {
		t.Fatal("cross-owner delete must fail")
	}

	// Delete one owner: file retained. Second: file removed.
	if err := svc.Delete(ctx, 1, da); err != nil {
		t.Fatal(err)
	}
	if err := env.DB.Get(&rc, `SELECT refcount FROM objects WHERE digest=$1`, digest(data)); err != nil {
		t.Fatalf("shared object gone while still referenced: %v", err)
	}
	if rc != 1 {
		t.Fatalf("refcount after first delete = %d want 1", rc)
	}
	if err := svc.Delete(ctx, 2, dbID); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := env.DB.Get(&n, `SELECT count(*) FROM objects WHERE digest=$1`, digest(data)); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatal("object row retained after last reference released")
	}
	if env.Store.ObjectExists(digest(data), int64(len(data))) {
		t.Fatal("shared file not cleaned after refcount hit zero")
	}
}

func TestAcquireReleaseRaceKeepsReferencedBlob(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)

	// A digest with one initial reference (one "dataset chunk").
	payload := []byte("race-condition-blob-payload-0123456789")
	dgst := digest(payload)

	acquire := func() (string, func(), error) {
		staging := t.TempDir() + "/st"
		if err := writeFile(staging, payload); err != nil {
			return "", nil, err
		}
		tx, err := env.DB.BeginTxx(ctx, nil)
		if err != nil {
			return "", nil, err
		}
		if _, err := env.Objects.Acquire(tx, dgst, int64(len(payload)), staging); err != nil {
			_ = tx.Rollback()
			return "", nil, err
		}
		return "", func() { _ = tx.Commit() }, nil
	}

	// Establish a baseline reference that is never released. Every worker
	// acquires its own fresh reference and releases that same reference, so the
	// net refcount change per worker is zero and the blob must never reach
	// zero even though the per-digest advisory lock interleaves the two.
	_, done1, err := acquire()
	if err != nil {
		t.Fatal(err)
	}
	done1()

	const iterations = 40
	var wg sync.WaitGroup
	start := make(chan struct{})
	errs := make(chan error, iterations)
	for i := 0; i < iterations; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start

			// Take a new reference.
			staging := t.TempDir() + "/st"
			if err := writeFile(staging, payload); err != nil {
				errs <- err
				return
			}
			tx, err := env.DB.BeginTxx(ctx, nil)
			if err != nil {
				errs <- err
				return
			}
			if _, err := env.Objects.Acquire(tx, dgst, int64(len(payload)), staging); err != nil {
				_ = tx.Rollback()
				errs <- err
				return
			}
			if err := tx.Commit(); err != nil {
				errs <- err
				return
			}

			// While this reference exists the blob must be readable.
			f, err := env.Store.OpenObject(dgst)
			if err != nil {
				errs <- err
				return
			}
			_ = f.Close()

			// Release the reference this worker took (not the baseline).
			tx2, err := env.DB.BeginTxx(ctx, nil)
			if err != nil {
				errs <- err
				return
			}
			p, err := env.Objects.ReleaseLocked(tx2, dgst)
			if err != nil {
				_ = tx2.Rollback()
				errs <- err
				return
			}
			if err := tx2.Commit(); err != nil {
				errs <- err
				return
			}
			if p != "" {
				_ = removeIfExists(p)
			}
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("race error: %v", err)
	}

	// Balanced acquires/releases leave refcount 1; blob must still be present
	// and readable (no in-use blob ever deleted).
	var rc int
	if err := env.DB.Get(&rc, `SELECT refcount FROM objects WHERE digest=$1`, dgst); err != nil {
		t.Fatalf("object vanished: %v", err)
	}
	if rc != 1 {
		t.Fatalf("refcount = %d want 1", rc)
	}
	f, err := env.Store.OpenObject(dgst)
	if err != nil {
		t.Fatalf("in-use blob missing: %v", err)
	}
	got, _ := io.ReadAll(f)
	f.Close()
	if !bytes.Equal(got, payload) {
		t.Fatal("blob corrupted during race")
	}
	// Directory sanity: no orphan files.
	known := func(d string) (bool, error) {
		var ok bool
		err := env.DB.Get(&ok, `SELECT EXISTS(SELECT 1 FROM objects WHERE digest=$1)`, d)
		return ok, err
	}
	orphans, err := env.Store.OrphanObjects(known)
	if err != nil || len(orphans) != 0 {
		t.Fatalf("orphans=%v err=%v", orphans, err)
	}
}

// helpers

func writeFile(path string, b []byte) error {
	return writeOSFile(path, b)
}

func removeIfExists(p string) error {
	return removeOSPath(p)
}
