package synapticgo_test

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"
)

func blobCount(t *testing.T, env *testEnv) (rows int, files []string) {
	t.Helper()
	if err := env.DB.Get(&rows, `SELECT count(*) FROM blobs`); err != nil {
		t.Fatalf("count blobs: %v", err)
	}
	entries, err := os.ReadDir(env.Store.BlobRootForTest())
	if err != nil {
		t.Fatalf("read blob dir: %v", err)
	}
	for _, shard := range entries {
		fs, err := os.ReadDir(env.Store.BlobRootForTest() + "/" + shard.Name())
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range fs {
			files = append(files, f.Name())
		}
	}
	return rows, files
}

func TestSharedContentDedupAndReferenceReclamation(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	// Two datasets with byte-identical content (same chunks + same whole).
	content := []byte("identical content shared between two datasets, chunked!!")
	id1, plan := uploadAndPublish(t, alice, "shared-a", content, nil)
	id2, _ := uploadAndPublish(t, alice, "shared-b", content, nil)

	var refcount int
	if err := env.DB.Get(&refcount,
		`SELECT refcount FROM blobs WHERE sha256 = $1`, plan.wholeSHA); err != nil {
		t.Fatalf("refcount: %v", err)
	}
	// Each dataset references the whole blob once (chunk blobs are also
	// shared, with refcount two from the paired chunk rows).
	if refcount != 2 {
		t.Fatalf("whole blob refcount=%d, want 2 (one per dataset)", refcount)
	}
	if _, files := blobCount(t, env); len(files) == 0 {
		t.Fatal("no blob files on disk")
	}

	// Delete first dataset: file must survive because dataset 2 references it.
	if code, b := alice.doJSON("DELETE", fmt.Sprintf("/v1/datasets/%d", id1), nil, nil); code != 204 {
		t.Fatalf("delete first: %d %s", code, b)
	}
	if _, err := env.Datasets.GarbageCollect(context.Background()); err != nil {
		t.Fatalf("gc: %v", err)
	}
	if _, err := os.Stat(env.Store.BlobPath(plan.wholeSHA)); os.IsNotExist(err) {
		t.Fatal("shared blob file removed while still referenced")
	}

	// Delete second dataset: now the shared file is truly unreferenced.
	if code, b := alice.doJSON("DELETE", fmt.Sprintf("/v1/datasets/%d", id2), nil, nil); code != 204 {
		t.Fatalf("delete second: %d %s", code, b)
	}
	if _, err := env.Datasets.GarbageCollect(context.Background()); err != nil {
		t.Fatalf("gc: %v", err)
	}
	if _, err := os.Stat(env.Store.BlobPath(plan.wholeSHA)); !os.IsNotExist(err) {
		t.Fatal("orphan blob file survived after last reference removed")
	}
	var n int
	if err := env.DB.Get(&n, `SELECT count(*) FROM blobs`); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("expected zero blob rows after full cleanup, got %d", n)
	}
}

func TestDeleteDatasetBlockedByModelReference(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	content := datasetJSON2D()
	id, plan := uploadAndPublish(t, alice, "train", content, nil)

	reg := registerLinearModel(t, alice, "m", "v1", 2, []string{"x", "y"},
		ptrInt64(int64(id)), nil, map[string]float64{"accuracy": 0.7})
	_ = reg // Delete must be refused while the model binds this dataset hash.
	if code, b := alice.doJSON("DELETE", fmt.Sprintf("/v1/datasets/%d", id), nil, nil); code != 409 {
		t.Fatalf("delete referenced dataset: code=%d body=%s, want 409", code, b)
	}
	// Delete model first, then dataset delete succeeds.
	var models []map[string]any
	code, body := alice.doJSON("GET", "/v1/models?name=m", nil, nil)
	if code != 200 {
		t.Fatalf("list models: %d %s", code, body)
	}
	mustJSON(body, &models)
	mid := int(models[0]["id"].(float64))
	if code, b := alice.doJSON("DELETE", fmt.Sprintf("/v1/models/%d", mid), nil, nil); code != 204 {
		t.Fatalf("delete model: %d %s", code, b)
	}
	if code, b := alice.doJSON("DELETE", fmt.Sprintf("/v1/datasets/%d", id), nil, nil); code != 204 {
		t.Fatalf("delete after model gone: %d %s", code, b)
	}
	_ = plan
}

func TestConcurrentPublishRaces(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	content := []byte("concurrent publish stress payload, deliberately chunked!!!")
	plan := makeChunks(content, 9)
	const n = 6
	ids := make([]int, n)
	for i := 0; i < n; i++ {
		ds := alice.createDataset(fmt.Sprintf("race-%d", i), plan, "")
		ids[i] = datasetID(ds)
		for j, ch := range plan.chunks {
			if code, b := alice.uploadChunk(ids[i], j, ch); code != 200 {
				t.Fatalf("upload ds%d chunk%d: %d %s", i, j, code, b)
			}
		}
	}

	// Publish every dataset twice concurrently: at most one merge per
	// dataset proceeds; retries either get 409 (already publishing) or the
	// idempotent ready result. Final state must always be ready with intact
	// content.
	var wg sync.WaitGroup
	errs := make(chan error, n*2)
	for _, id := range ids {
		for k := 0; k < 2; k++ {
			wg.Add(1)
			go func(id int) {
				defer wg.Done()
				code, v := alice.publish(id)
				switch code {
				case 200:
					if v["status"] != "ready" {
						errs <- fmt.Errorf("ds%d status %v", id, v["status"])
					}
				case 409:
					// lost the race to the other concurrent publish
				default:
					errs <- fmt.Errorf("ds%d unexpected code %d", id, code)
				}
			}(id)
		}
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
	for _, id := range ids {
		eventually(t, 3*time.Second, func() error {
			code, v := alice.publish(id)
			if code != 200 || v["status"] != "ready" {
				return fmt.Errorf("not ready: %d %v", code, v["status"])
			}
			return nil
		})
		code, b := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil)
		if code != 200 || string(b) != string(content) {
			t.Fatalf("ds%d content corrupted by concurrent publish", id)
		}
	}
}

func TestPublishVsGarbageCollectionRace(t *testing.T) {
	env := newTestEnv(t)

	// Hammer: many users upload+publish distinct datasets while a GC loop
	// runs continuously. No ready dataset may ever lose its blob file.
	ctx, cancel := context.WithCancel(context.Background())
	var wgGC sync.WaitGroup
	wgGC.Add(1)
	go func() {
		defer wgGC.Done()
		for {
			select {
			case <-ctx.Done():
				return
			default:
				_, _ = env.Datasets.GarbageCollect(ctx)
			}
		}
	}()

	const datasets = 12
	var wg sync.WaitGroup
	for i := 0; i < datasets; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			user := env.registerCachedUser(fmt.Sprintf("racer-%d", i))
			// Content repeats so dedup/GC interleave heavily.
			content := []byte(fmt.Sprintf("shared race body number %02d padding padding", i%4))
			uploadAndPublish(t, user, fmt.Sprintf("d-%d", i), content, nil)
		}(i)
	}
	wg.Wait()
	cancel()
	wgGC.Wait()

	// Every created dataset must still be readable (its references survived).
	for i := 0; i < datasets; i++ {
		user := &client{env: env, token: tokenFor(t, env, fmt.Sprintf("racer-%d", i))}
		var list []map[string]any
		code, body := user.doJSON("GET", "/v1/datasets", nil, nil)
		if code != 200 {
			t.Fatalf("list racer %d: %d %s", i, code, body)
		}
		mustJSON(body, &list)
		if len(list) != 1 || list[0]["status"] != "ready" {
			t.Fatalf("racer %d dataset not ready: %v", i, list)
		}
		id := int(list[0]["id"].(float64))
		if code, _ := user.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil); code != 200 {
			t.Fatalf("racer %d content vanished (GC race)", i)
		}
	}
}

func TestCrossUserIsolation(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")
	bob := env.registerUser("bob")

	content := []byte("alice secret training data, chunked for testing!!!!")
	id, _ := uploadAndPublish(t, alice, "secret", content, nil)

	// Bob cannot see, read, chunk into, publish, or delete Alice's dataset.
	for _, tc := range []struct {
		method string
		path   string
	}{
		{"GET", fmt.Sprintf("/v1/datasets/%d", id)},
		{"GET", fmt.Sprintf("/v1/datasets/%d/content", id)},
		{"DELETE", fmt.Sprintf("/v1/datasets/%d", id)},
		{"POST", fmt.Sprintf("/v1/datasets/%d/publish", id)},
	} {
		if code, _ := bob.doRaw(tc.method, tc.path, nil); code != 404 {
			t.Fatalf("bob %s %s: want 404 (existence hidden), got %d", tc.method, tc.path, code)
		}
	}
	if code, _ := bob.doRaw("PUT", fmt.Sprintf("/v1/datasets/%d/chunks/0", id), []byte("xxxx")); code != 404 {
		t.Fatalf("bob chunk upload: want 404, got %d", code)
	}
	if code, b := bob.doJSON("GET", "/v1/datasets", nil, nil); code != 200 {
		t.Fatalf("bob list: %d %s", code, b)
	} else {
		var list []any
		mustJSON(b, &list)
		if len(list) != 0 {
			t.Fatalf("bob sees %d datasets, want 0", len(list))
		}
	}
	// Alice still has her data intact despite Bob's attempts.
	if code, b := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil); code != 200 || string(b) != string(content) {
		t.Fatalf("alice content compromised: code=%d", code)
	}
}

// ptrInt64 returns a pointer for optional request fields.
func ptrInt64(v int64) *int64 { return &v }
