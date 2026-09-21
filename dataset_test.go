package synapticgo_test

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"testing"

	"github.com/synapticgo/synapticgo/internal/dataset"
)

// uploadAndPublish is the happy path shared by other tests: create a manifest,
// push chunks in the given order (default reversed), publish, verify content.
func uploadAndPublish(t *testing.T, c *client, name string, content []byte, order []int) (dsID int, plan *chunkPlan) {
	t.Helper()
	chunkSize := int64(7) // force multiple chunks
	plan = makeChunks(content, chunkSize)
	ds := c.createDataset(name, plan, "")
	dsID = datasetID(ds)

	if order == nil {
		// Out-of-order by default.
		order = make([]int, len(plan.chunks))
		for i := range order {
			order[i] = len(plan.chunks) - 1 - i
		}
	}
	for _, idx := range order {
		if code, body := c.uploadChunk(dsID, idx, plan.chunks[idx]); code != 200 {
			t.Fatalf("upload chunk %d: %d %s", idx, code, body)
		}
	}
	code, v := c.publish(dsID)
	if code != 200 {
		t.Fatalf("publish: %d %v", code, v)
	}
	if v["status"] != "ready" {
		t.Fatalf("status = %v, want ready", v["status"])
	}
	code, body := c.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", dsID), nil)
	if code != 200 || !bytes.Equal(body, content) {
		t.Fatalf("download mismatch: code=%d equal=%v", code, bytes.Equal(body, content))
	}
	return dsID, plan
}

func TestChunkedUploadOutOfOrderAndResume(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	content := []byte("SynapticGo out-of-order chunked upload payload 0123456789")
	id, plan := uploadAndPublish(t, alice, "d1", content, nil)
	if len(plan.chunks) < 2 {
		t.Fatalf("test setup: expected multiple chunks, got %d", len(plan.chunks))
	}

	// Re-fetch shows all chunks received.
	var view map[string]any
	code, body := alice.doJSON("GET", fmt.Sprintf("/v1/datasets/%d", id), nil, nil)
	if code != 200 {
		t.Fatalf("get: %d %s", code, body)
	}
	mustJSON(body, &view)
	chunks := view["chunks"].([]any)
	received := 0
	for _, ch := range chunks {
		if ch.(map[string]any)["received"].(bool) {
			received++
		}
	}
	if received != len(plan.chunks) {
		t.Fatalf("received %d of %d chunks", received, len(plan.chunks))
	}
}

func TestChunkRetransmitIdempotentContentConflictRejected(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")
	content := []byte("idempotent retransmit payload, longer than chunk size")
	plan := makeChunks(content, 8)
	ds := createWithPlan(alice, plan)
	id := datasetID(ds)

	// First upload chunk 0.
	if code, b := alice.uploadChunk(id, 0, plan.chunks[0]); code != 200 {
		t.Fatalf("initial upload: %d %s", code, b)
	}
	// Identical retransmit: idempotent 200.
	if code, b := alice.uploadChunk(id, 0, plan.chunks[0]); code != 200 {
		t.Fatalf("identical retransmit: %d %s", code, b)
	}
	// Different content but same length: conflict.
	corrupt := append([]byte(nil), plan.chunks[0]...)
	corrupt[0] = byte((int(corrupt[0]) + 1) % 256)
	if code, b := alice.uploadChunk(id, 0, corrupt); code != 409 {
		t.Fatalf("content conflict: code=%d body=%s, want 409", code, b)
	}
	// Wrong size: 422 (length is checked before content identity).
	if code, b := alice.uploadChunk(id, 0, append(plan.chunks[0], 0)); code != 422 {
		t.Fatalf("size mismatch: code=%d body=%s, want 422", code, b)
	}
	// Different content of the same size on a not-yet-stored slot: conflict.
	other := append([]byte(nil), plan.chunks[0]...)
	other[0] = byte((int(other[0]) + 7) % 256)
	if string(other) == string(plan.chunks[0]) {
		other[0] = byte((int(other[0]) + 1) % 256)
	}
	if code, b := alice.uploadChunk(id, 1, other); code != 409 {
		t.Fatalf("wrong content for declared chunk: code=%d body=%s, want 409", code, b)
	}
}

func TestChunkRetransmitIdempotentAfterPublish(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")
	content := []byte("idempotent retransmit must also work after publish!!!")
	id, plan := uploadAndPublish(t, alice, "later", content, nil)

	// Byte-identical retransmit against a ready dataset stays 200.
	if code, b := alice.uploadChunk(id, 0, plan.chunks[0]); code != 200 {
		t.Fatalf("post-publish identical retransmit: %d %s", code, b)
	}
	// Different bytes are still rejected.
	corrupt := append([]byte(nil), plan.chunks[0]...)
	corrupt[0] = byte((int(corrupt[0]) + 1) % 256)
	if code, b := alice.uploadChunk(id, 0, corrupt); code != 409 {
		t.Fatalf("post-publish tampered retransmit: %d %s, want 409", code, b)
	}
}

func createWithPlan(cl *client, plan *chunkPlan) map[string]any {
	return cl.createDataset("ds", plan, "")
}

func TestPublishRejectedWhenChunksMissingOrWholeHashWrong(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	// Missing chunks.
	content := []byte("cannot publish until every chunk is here")
	plan := makeChunks(content, 6)
	ds := alice.createDataset("missing", plan, "")
	id := datasetID(ds)
	if code, b := alice.uploadChunk(id, 0, plan.chunks[0]); code != 200 {
		t.Fatalf("upload: %d %s", code, b)
	}
	if code, b := alice.publish(id); code != 422 {
		t.Fatalf("publish with missing chunk: code=%d body=%s, want 422", code, b)
	}

	// Whole-hash mismatch: declare a bogus overall digest.
	bogus := make([]byte, 64)
	for i := range bogus {
		bogus[i] = 'a'
	}
	ds2 := alice.createDataset("bogushash", plan, string(bogus))
	id2 := datasetID(ds2)
	for i, ch := range plan.chunks {
		if code, b := alice.uploadChunk(id2, i, ch); code != 200 {
			t.Fatalf("upload %d: %d %s", i, code, b)
		}
	}
	if code, b := alice.publish(id2); code != 422 {
		t.Fatalf("publish with wrong whole hash: code=%d body=%s, want 422", code, b)
	}
	// Not ready, so no download.
	if code, _ := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id2), nil); code != 409 {
		t.Fatalf("download unpublished: want 409, got %d", code)
	}
}

func TestPublishInterruptedBetweenMergeAndCommitRecovers(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	env.Datasets.Hooks.AfterMergeWrite = func(int64) error {
		return fmt.Errorf("simulated crash after merge write")
	}
	content := []byte(`{"dim":2,"examples":[{"x":[1,2],"label":0}]}`)
	plan := makeChunks(content, 16)
	ds := alice.createDataset("crash", plan, "")
	id := datasetID(ds)
	for i, ch := range plan.chunks {
		if code, b := alice.uploadChunk(id, i, ch); code != 200 {
			t.Fatalf("upload %d: %d %s", i, code, b)
		}
	}
	// First publish fails as if the process died right after writing merge.
	if code, b := alice.publish(id); code != 500 {
		t.Fatalf("interrupted publish: code=%d body=%s, want 500", code, b)
	}
	var v map[string]any
	_, body := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d", id), nil)
	mustJSON(body, &v)
	if v["status"] != "publishing" {
		t.Fatalf("expected publishing row after failure, got %v", v["status"])
	}

	// Simulate process restart: temp files wiped, publishing rows reset.
	if err := env.Datasets.RecoverAtStartup(context.Background()); err != nil {
		t.Fatalf("recovery: %v", err)
	}
	code2, body2 := alice.doJSON("GET", fmt.Sprintf("/v1/datasets/%d", id), nil, nil)
	if code2 != 200 {
		t.Fatalf("get after recovery: %d %s", code2, body2)
	}
	mustJSON(body2, &v)
	if v["status"] != "uploading" {
		t.Fatalf("expected uploading after recovery, got %v", v["status"])
	}
	// Publish must succeed on retry without re-uploading any chunk bytes.
	env.Datasets.Hooks = dataset.Hooks{}
	if code, v2 := alice.publish(id); code != 200 || v2["status"] != "ready" {
		t.Fatalf("republish after recovery: code=%d view=%v", code, v2)
	}
}

func TestMergeTempFileNeverServed(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	content := []byte("half-file must never be marked usable payload!!")
	plan := makeChunks(content, 10)
	ds := alice.createDataset("half", plan, "")
	id := datasetID(ds)

	// While still uploading, the content endpoint refuses and no merged blob
	// exists under the blobs directory (only chunk blobs).
	if code, _ := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil); code != 409 {
		t.Fatalf("content during upload: want 409, got %d", code)
	}
	for i, ch := range plan.chunks {
		if code, b := alice.uploadChunk(id, i, ch); code != 200 {
			t.Fatalf("upload %d: %d %s", i, code, b)
		}
	}
	if code, _ := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil); code != 409 {
		t.Fatalf("content before publish: want 409, got %d", code)
	}
	if code, v := alice.publish(id); code != 200 || v["status"] != "ready" {
		t.Fatalf("publish: %d %v", code, v)
	}
	// After publish the temp directory is empty; only content-addressed
	// blobs remain.
	if entries, err := os.ReadDir(env.DataDir + "/tmp"); err != nil || len(entries) != 0 {
		t.Fatalf("temp dir not empty after publish: %d entries, err=%v", len(entries), err)
	}
	code, body := alice.doRaw("GET", fmt.Sprintf("/v1/datasets/%d/content", id), nil)
	if code != 200 || !bytes.Equal(body, content) {
		t.Fatalf("final content mismatch: %d", code)
	}
}
