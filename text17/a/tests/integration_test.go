// Package tests contains end-to-end tests against a real PostgreSQL.
// Run with:
//
//	TEST_DATABASE_URL="postgres://postgres:postgres@localhost:5432/synapticgo_test?sslmode=disable" go test ./tests/
package tests

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"synapticgo/internal/app"
	"synapticgo/internal/httpapi"
	"synapticgo/internal/store"
)

const (
	alice = "alice"
	bob   = "bob"
)

type env struct {
	t  *testing.T
	e  *echo.Echo
	db *sqlx.DB
	fs *app.FileStore
}

func setup(t *testing.T) *env {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	db, err := sqlx.Connect("postgres", dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
	if err := store.Migrate(context.Background(), db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	fs, err := app.NewFileStore(t.TempDir())
	if err != nil {
		t.Fatalf("filestore: %v", err)
	}
	return &env{t: t, e: httpapi.New(db, fs), db: db, fs: fs}
}

func (env *env) req(method, path, user string, headers map[string]string, body any) *httptest.ResponseRecorder {
	env.t.Helper()
	var rdr *bytes.Reader
	switch b := body.(type) {
	case nil:
		rdr = bytes.NewReader(nil)
	case []byte:
		rdr = bytes.NewReader(b)
	default:
		data, err := json.Marshal(body)
		if err != nil {
			env.t.Fatalf("marshal body: %v", err)
		}
		rdr = bytes.NewReader(data)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if user != "" {
		req.Header.Set("X-User-ID", user)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	env.e.ServeHTTP(rec, req)
	return rec
}

func (env *env) mustStatus(rec *httptest.ResponseRecorder, want int) {
	env.t.Helper()
	if rec.Code != want {
		env.t.Fatalf("status = %d, want %d; body: %s", rec.Code, want, rec.Body.String())
	}
}

func decode(t *testing.T, rec *httptest.ResponseRecorder, v any) {
	t.Helper()
	if err := json.Unmarshal(rec.Body.Bytes(), v); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// makeContent builds deterministic pseudo-random content.
func makeContent(n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = byte(i * 2654435761 % 251)
	}
	return out
}

func (env *env) createDataset(user, name string, content []byte, chunkSize int64) app.Dataset {
	env.t.Helper()
	rec := env.req(http.MethodPost, "/datasets", user, nil, map[string]any{
		"name":       name,
		"total_size": len(content),
		"chunk_size": chunkSize,
		"sha256":     sha256Hex(content),
	})
	env.mustStatus(rec, http.StatusCreated)
	var ds app.Dataset
	decode(env.t, rec, &ds)
	return ds
}

func (env *env) uploadChunk(user string, datasetID int64, content []byte, chunkSize int64, index int) *httptest.ResponseRecorder {
	env.t.Helper()
	off := int64(index) * chunkSize
	end := off + chunkSize
	if end > int64(len(content)) {
		end = int64(len(content))
	}
	chunk := content[off:end]
	return env.req(http.MethodPut, fmt.Sprintf("/datasets/%d/chunks/%d", datasetID, index), user,
		map[string]string{"X-Chunk-SHA256": sha256Hex(chunk)}, chunk)
}

func (env *env) publishDataset(user string, datasetID int64) *httptest.ResponseRecorder {
	env.t.Helper()
	return env.req(http.MethodPost, fmt.Sprintf("/datasets/%d/publish", datasetID), user, nil, nil)
}

// publishFull creates a dataset, uploads all chunks in order, and publishes.
func (env *env) publishFull(user, name string, content []byte, chunkSize int64) app.Dataset {
	env.t.Helper()
	ds := env.createDataset(user, name, content, chunkSize)
	for i := 0; i < ds.ChunkCount; i++ {
		env.mustStatus(env.uploadChunk(user, ds.ID, content, chunkSize, i), http.StatusCreated)
	}
	rec := env.publishDataset(user, ds.ID)
	env.mustStatus(rec, http.StatusOK)
	decode(env.t, rec, &ds)
	if ds.Status != app.DatasetStatusPublished {
		env.t.Fatalf("dataset status = %q, want published", ds.Status)
	}
	return ds
}

func (env *env) createModel(user, name string) app.Model {
	env.t.Helper()
	rec := env.req(http.MethodPost, "/models", user, nil, map[string]any{"name": name})
	env.mustStatus(rec, http.StatusCreated)
	var m app.Model
	decode(env.t, rec, &m)
	return m
}

func (env *env) createVersion(user string, modelID, datasetID int64, labels []string, weights [][]float64, bias []float64) app.ModelVersion {
	env.t.Helper()
	rec := env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions", modelID), user, nil, map[string]any{
		"dataset_id": datasetID,
		"input_dim":  len(weights[0]),
		"labels":     labels,
		"weights":    weights,
		"bias":       bias,
	})
	env.mustStatus(rec, http.StatusCreated)
	var mv app.ModelVersion
	decode(env.t, rec, &mv)
	return mv
}

func (env *env) createExperiment(user, name string, versionID, datasetID int64, metrics map[string]any) app.Experiment {
	env.t.Helper()
	rec := env.req(http.MethodPost, "/experiments", user, nil, map[string]any{
		"name":             name,
		"model_version_id": versionID,
		"dataset_id":       datasetID,
		"metrics":          metrics,
	})
	env.mustStatus(rec, http.StatusCreated)
	var exp app.Experiment
	decode(env.t, rec, &exp)
	return exp
}

// --- tests ---

func TestChunkUploadOutOfOrderAndIdempotent(t *testing.T) {
	env := setup(t)
	content := makeContent(2500)
	ds := env.createDataset(alice, "ds", content, 1000)
	if ds.ChunkCount != 3 {
		t.Fatalf("chunk_count = %d, want 3", ds.ChunkCount)
	}

	// Out-of-order upload: 2, 0, 1.
	for _, idx := range []int{2, 0, 1} {
		env.mustStatus(env.uploadChunk(alice, ds.ID, content, 1000, idx), http.StatusCreated)
	}
	// Retransmitting identical content is idempotent.
	rec := env.uploadChunk(alice, ds.ID, content, 1000, 0)
	env.mustStatus(rec, http.StatusOK)

	// Same index, different content -> conflict.
	bad := make([]byte, 1000)
	rec = env.req(http.MethodPut, fmt.Sprintf("/datasets/%d/chunks/0", ds.ID), alice,
		map[string]string{"X-Chunk-SHA256": sha256Hex(bad)}, bad)
	env.mustStatus(rec, http.StatusConflict)

	// Declared hash not matching the body -> rejected.
	rec = env.req(http.MethodPut, fmt.Sprintf("/datasets/%d/chunks/1", ds.ID), alice,
		map[string]string{"X-Chunk-SHA256": sha256Hex(bad)}, content[1000:2000])
	env.mustStatus(rec, http.StatusBadRequest)

	// Status endpoint reports what is missing for resume.
	rec = env.req(http.MethodGet, fmt.Sprintf("/datasets/%d", ds.ID), alice, nil, nil)
	env.mustStatus(rec, http.StatusOK)
	var view app.DatasetView
	decode(t, rec, &view)
	if len(view.UploadedChunks) != 3 || len(view.MissingChunks) != 0 {
		t.Fatalf("uploaded=%v missing=%v, want 3 uploaded 0 missing", view.UploadedChunks, view.MissingChunks)
	}

	rec = env.publishDataset(alice, ds.ID)
	env.mustStatus(rec, http.StatusOK)
	var published app.Dataset
	decode(t, rec, &published)
	if published.Status != app.DatasetStatusPublished || published.FileID == nil {
		t.Fatalf("unexpected publish result: %+v", published)
	}
	// Merged content must be intact and content-addressed.
	data, err := os.ReadFile(env.fs.FilePath(sha256Hex(content)))
	if err != nil {
		t.Fatalf("read merged file: %v", err)
	}
	if !bytes.Equal(data, content) {
		t.Fatal("merged file content differs from uploaded content")
	}
}

func TestPublishRequiresAllChunksAndMatchingDigest(t *testing.T) {
	env := setup(t)
	content := makeContent(2000)

	// Missing chunk -> cannot publish.
	ds := env.createDataset(alice, "partial", content, 1000)
	env.mustStatus(env.uploadChunk(alice, ds.ID, content, 1000, 0), http.StatusCreated)
	env.mustStatus(env.publishDataset(alice, ds.ID), http.StatusConflict)

	// Overall digest mismatch -> cannot publish.
	other := makeContent(2000)
	other[0] ^= 0xff
	rec := env.req(http.MethodPost, "/datasets", alice, nil, map[string]any{
		"name":       "wrong-digest",
		"total_size": len(content),
		"chunk_size": 1000,
		"sha256":     sha256Hex(other), // declares digest of different content
	})
	env.mustStatus(rec, http.StatusCreated)
	var ds2 app.Dataset
	decode(t, rec, &ds2)
	for i := 0; i < 2; i++ {
		env.mustStatus(env.uploadChunk(alice, ds2.ID, content, 1000, i), http.StatusCreated)
	}
	env.mustStatus(env.publishDataset(alice, ds2.ID), http.StatusConflict)
	// Still not published afterwards.
	rec = env.req(http.MethodGet, fmt.Sprintf("/datasets/%d", ds2.ID), alice, nil, nil)
	var view app.DatasetView
	decode(t, rec, &view)
	if view.Status != app.DatasetStatusUploading {
		t.Fatalf("status = %q, want uploading after failed publish", view.Status)
	}
}

func TestConcurrentPublish(t *testing.T) {
	env := setup(t)
	content := makeContent(3000)
	ds := env.createDataset(alice, "race", content, 1000)
	for i := 0; i < ds.ChunkCount; i++ {
		env.mustStatus(env.uploadChunk(alice, ds.ID, content, 1000, i), http.StatusCreated)
	}

	const n = 8
	codes := make([]int, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			codes[i] = env.publishDataset(alice, ds.ID).Code
		}(i)
	}
	wg.Wait()
	for i, code := range codes {
		if code != http.StatusOK {
			t.Fatalf("publish %d returned %d, all concurrent publishes must succeed idempotently", i, code)
		}
	}
	var fileRows int
	if err := env.db.Get(&fileRows, `SELECT count(*) FROM files WHERE sha256=$1`, sha256Hex(content)); err != nil {
		t.Fatal(err)
	}
	if fileRows != 1 {
		t.Fatalf("files rows for content = %d, want exactly 1", fileRows)
	}
}

func TestRecoveryAfterCrash(t *testing.T) {
	env := setup(t)
	content := makeContent(2000)
	ds := env.createDataset(alice, "crash", content, 1000)
	for i := 0; i < ds.ChunkCount; i++ {
		env.mustStatus(env.uploadChunk(alice, ds.ID, content, 1000, i), http.StatusCreated)
	}

	// Simulate crash leftovers: an interrupted publish state, a half-written
	// merge output, and an orphaned content file with no database row.
	if _, err := env.db.Exec(`UPDATE datasets SET status='merging' WHERE id=$1`, ds.ID); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(env.fs.MergeTmpPath(ds.ID), []byte("half-written"), 0o644); err != nil {
		t.Fatal(err)
	}
	orphanSHA := strings.Repeat("ab", 32)
	if err := os.WriteFile(env.fs.FilePath(orphanSHA), []byte("orphan"), 0o644); err != nil {
		t.Fatal(err)
	}

	svc := &app.DatasetService{DB: env.db, FS: env.fs}
	if err := svc.Recover(context.Background()); err != nil {
		t.Fatalf("recover: %v", err)
	}

	var status string
	if err := env.db.Get(&status, `SELECT status FROM datasets WHERE id=$1`, ds.ID); err != nil {
		t.Fatal(err)
	}
	if status != app.DatasetStatusUploading {
		t.Fatalf("status after recovery = %q, want uploading", status)
	}
	if _, err := os.Stat(env.fs.MergeTmpPath(ds.ID)); !os.IsNotExist(err) {
		t.Fatal("half-written merge file survived recovery")
	}
	if _, err := os.Stat(env.fs.FilePath(orphanSHA)); !os.IsNotExist(err) {
		t.Fatal("orphan content file survived recovery")
	}

	// The dataset must still be publishable; the interrupted attempt must not
	// have marked anything usable.
	rec := env.publishDataset(alice, ds.ID)
	env.mustStatus(rec, http.StatusOK)
}

func TestSharedFileRefCount(t *testing.T) {
	env := setup(t)
	content := makeContent(1500)
	dsA := env.publishFull(alice, "a", content, 1000)
	dsB := env.publishFull(alice, "b", content, 1000)

	if dsA.FileID == nil || dsB.FileID == nil || *dsA.FileID != *dsB.FileID {
		t.Fatalf("identical content should share one file: %+v %+v", dsA.FileID, dsB.FileID)
	}
	var fileRows int
	env.db.Get(&fileRows, `SELECT count(*) FROM files WHERE sha256=$1`, sha256Hex(content))
	if fileRows != 1 {
		t.Fatalf("files rows = %d, want 1", fileRows)
	}
	path := env.fs.FilePath(sha256Hex(content))

	// Deleting one dataset keeps the shared file.
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", dsA.ID), alice, nil, nil), http.StatusNoContent)
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("shared file removed while still referenced: %v", err)
	}
	env.db.Get(&fileRows, `SELECT count(*) FROM files WHERE sha256=$1`, sha256Hex(content))
	if fileRows != 1 {
		t.Fatalf("files rows after first delete = %d, want 1", fileRows)
	}

	// Deleting the last reference collects the file.
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", dsB.ID), alice, nil, nil), http.StatusNoContent)
	env.db.Get(&fileRows, `SELECT count(*) FROM files WHERE sha256=$1`, sha256Hex(content))
	if fileRows != 0 {
		t.Fatalf("files rows after last delete = %d, want 0", fileRows)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("unreferenced file not collected from disk")
	}
}

func TestDeleteChecksReferences(t *testing.T) {
	env := setup(t)
	content := makeContent(1000)
	ds := env.publishFull(alice, "ds", content, 1000)
	m := env.createModel(alice, "m")
	mv := env.createVersion(alice, m.ID, ds.ID, []string{"x", "y"}, [][]float64{{1, 0}, {0, 1}}, []float64{0, 0})

	// Dataset referenced by a model version cannot be deleted.
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", ds.ID), alice, nil, nil), http.StatusConflict)

	exp := env.createExperiment(alice, "e1", mv.ID, ds.ID, map[string]any{"accuracy": 0.9})
	// Model referenced by an experiment cannot be deleted.
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/models/%d", m.ID), alice, nil, nil), http.StatusConflict)

	// Remove the experiment, then the model, then the dataset.
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/experiments/%d", exp.ID), alice, nil, nil), http.StatusNoContent)
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/models/%d", m.ID), alice, nil, nil), http.StatusNoContent)
	env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", ds.ID), alice, nil, nil), http.StatusNoContent)
}

func TestCrossUserIsolation(t *testing.T) {
	env := setup(t)
	content := makeContent(1000)
	ds := env.publishFull(alice, "ds", content, 1000)
	m := env.createModel(alice, "m")
	mv := env.createVersion(alice, m.ID, ds.ID, []string{"x", "y"}, [][]float64{{1, 0}, {0, 1}}, []float64{0, 0})

	// Bob cannot see, modify, delete, or use Alice's resources.
	for _, tc := range []struct {
		method, path string
	}{
		{http.MethodGet, fmt.Sprintf("/datasets/%d", ds.ID)},
		{http.MethodDelete, fmt.Sprintf("/datasets/%d", ds.ID)},
		{http.MethodPost, fmt.Sprintf("/datasets/%d/publish", ds.ID)},
		{http.MethodGet, fmt.Sprintf("/models/%d", m.ID)},
		{http.MethodDelete, fmt.Sprintf("/models/%d", m.ID)},
		{http.MethodGet, fmt.Sprintf("/models/%d/versions/%d", m.ID, mv.Version)},
		{http.MethodPost, fmt.Sprintf("/models/%d/versions/%d/predict", m.ID, mv.Version)},
	} {
		rec := env.req(tc.method, tc.path, bob, nil, map[string]any{"inputs": [][]float64{{1, 2}}})
		env.mustStatus(rec, http.StatusNotFound)
	}

	// Bob cannot register a version on Alice's model or an experiment on
	// Alice's version/dataset.
	rec := env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions", m.ID), bob, nil, map[string]any{
		"dataset_id": ds.ID, "input_dim": 2, "labels": []string{"x", "y"},
		"weights": [][]float64{{1, 0}, {0, 1}}, "bias": []float64{0, 0},
	})
	env.mustStatus(rec, http.StatusNotFound)
	rec = env.req(http.MethodPost, "/experiments", bob, nil, map[string]any{
		"name": "e", "model_version_id": mv.ID, "dataset_id": ds.ID,
	})
	env.mustStatus(rec, http.StatusNotFound)

	// Listings are scoped per owner.
	rec = env.req(http.MethodGet, "/datasets", bob, nil, nil)
	env.mustStatus(rec, http.StatusOK)
	var list struct {
		Datasets []app.Dataset `json:"datasets"`
	}
	decode(t, rec, &list)
	if len(list.Datasets) != 0 {
		t.Fatalf("bob sees %d datasets, want 0", len(list.Datasets))
	}

	// Unauthenticated requests are rejected.
	rec = env.req(http.MethodGet, "/datasets", "", nil, nil)
	env.mustStatus(rec, http.StatusUnauthorized)
}

func TestPredictNumericCorrectness(t *testing.T) {
	env := setup(t)
	content := makeContent(500)
	ds := env.publishFull(alice, "ds", content, 500)
	m := env.createModel(alice, "m")
	labels := []string{"cat", "dog"}
	weights := [][]float64{{1, 0, -1}, {0, 1, 1}}
	bias := []float64{0.5, -0.5}
	mv := env.createVersion(alice, m.ID, ds.ID, labels, weights, bias)

	inputs := [][]float64{{2, 1, 0}, {0, 0, 0}}
	rec := env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions/%d/predict", m.ID, mv.Version),
		alice, nil, map[string]any{"inputs": inputs})
	env.mustStatus(rec, http.StatusOK)
	var resp struct {
		Predictions []struct {
			Index         int       `json:"index"`
			Label         string    `json:"label"`
			Probabilities []float64 `json:"probabilities"`
		} `json:"predictions"`
	}
	decode(t, rec, &resp)
	if len(resp.Predictions) != 2 {
		t.Fatalf("got %d predictions, want 2", len(resp.Predictions))
	}
	for i, x := range inputs {
		// Recompute logits = x·Wᵀ + b and softmax independently.
		logits := make([]float64, len(labels))
		for j := range labels {
			logits[j] = bias[j]
			for k, xv := range x {
				logits[j] += xv * weights[j][k]
			}
		}
		max := math.Max(logits[0], logits[1])
		den := math.Exp(logits[0]-max) + math.Exp(logits[1]-max)
		for j := range labels {
			want := math.Exp(logits[j]-max) / den
			got := resp.Predictions[i].Probabilities[j]
			if math.Abs(got-want) > 1e-12 {
				t.Errorf("input %d class %d: p = %v, want %v", i, j, got, want)
			}
		}
	}
	if resp.Predictions[0].Label != "cat" {
		t.Errorf("prediction 0 label = %q, want cat", resp.Predictions[0].Label)
	}

	// Shape violations are rejected, not silently answered.
	rec = env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions/%d/predict", m.ID, mv.Version),
		alice, nil, map[string]any{"inputs": [][]float64{{1, 2}}})
	env.mustStatus(rec, http.StatusBadRequest)
	rec = env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions/%d/predict", m.ID, mv.Version),
		alice, nil, map[string]any{"inputs": [][]float64{}})
	env.mustStatus(rec, http.StatusBadRequest)

	// Invalid model parameters are rejected at registration.
	rec = env.req(http.MethodPost, fmt.Sprintf("/models/%d/versions", m.ID), alice, nil, map[string]any{
		"dataset_id": ds.ID, "input_dim": 3, "labels": labels,
		"weights": [][]float64{{1, 0}, {0, 1}}, "bias": bias, // wrong weight width
	})
	env.mustStatus(rec, http.StatusBadRequest)
}

func TestCompareVersions(t *testing.T) {
	env := setup(t)
	content := makeContent(500)
	ds := env.publishFull(alice, "ds", content, 500)
	m := env.createModel(alice, "m")
	labels := []string{"a", "b"}
	v1 := env.createVersion(alice, m.ID, ds.ID, labels, [][]float64{{1, 0}, {0, 1}}, []float64{0, 0})
	v2 := env.createVersion(alice, m.ID, ds.ID, labels, [][]float64{{2, 0}, {0, 2}}, []float64{0, 0})
	env.createExperiment(alice, "e1", v1.ID, ds.ID, map[string]any{"accuracy": 0.8})
	env.createExperiment(alice, "e2", v2.ID, ds.ID, map[string]any{"accuracy": 0.9})

	rec := env.req(http.MethodGet, fmt.Sprintf("/models/%d/compare?versions=1,2", m.ID), alice, nil, nil)
	env.mustStatus(rec, http.StatusOK)
	var cmp app.CompareResult
	decode(t, rec, &cmp)
	if !cmp.Comparable {
		t.Fatalf("same label tables must be comparable: %s", cmp.Reason)
	}
	if diff := cmp.MetricDiffs["accuracy"]; math.Abs(diff-0.1) > 1e-9 {
		t.Errorf("accuracy diff = %v, want 0.1", diff)
	}

	// A version with a different label table is not comparable.
	v3 := env.createVersion(alice, m.ID, ds.ID, []string{"a", "b", "c"},
		[][]float64{{1, 0}, {0, 1}, {1, 1}}, []float64{0, 0, 0})
	env.createExperiment(alice, "e3", v3.ID, ds.ID, map[string]any{"accuracy": 0.99})
	rec = env.req(http.MethodGet, fmt.Sprintf("/models/%d/compare?versions=1,3", m.ID), alice, nil, nil)
	env.mustStatus(rec, http.StatusOK)
	var cmp2 app.CompareResult
	decode(t, rec, &cmp2)
	if cmp2.Comparable {
		t.Fatal("different label tables must not be comparable")
	}
	if cmp2.MetricDiffs != nil {
		t.Errorf("metric diffs must be omitted for incomparable versions, got %v", cmp2.MetricDiffs)
	}
	if len(cmp2.Versions) != 2 {
		t.Fatalf("compare returned %d versions, want 2", len(cmp2.Versions))
	}
}

func TestCleanupRacesWithNewReference(t *testing.T) {
	env := setup(t)
	content := makeContent(1200)
	sha := sha256Hex(content)

	// Repeatedly: one dataset is published, then concurrently deleted while a
	// second dataset with identical content is published. The shared file
	// must never be deleted out from under the new reference.
	for round := 0; round < 10; round++ {
		dsA := env.publishFull(alice, fmt.Sprintf("a-%d", round), content, 600)
		dsB := env.createDataset(alice, fmt.Sprintf("b-%d", round), content, 600)
		for i := 0; i < dsB.ChunkCount; i++ {
			env.mustStatus(env.uploadChunk(alice, dsB.ID, content, 600, i), http.StatusCreated)
		}

		var wg sync.WaitGroup
		var delCode, pubCode int
		wg.Add(2)
		go func() {
			defer wg.Done()
			delCode = env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", dsA.ID), alice, nil, nil).Code
		}()
		go func() {
			defer wg.Done()
			pubCode = env.publishDataset(alice, dsB.ID).Code
		}()
		wg.Wait()

		if delCode != http.StatusNoContent {
			t.Fatalf("round %d: delete returned %d", round, delCode)
		}
		if pubCode != http.StatusOK {
			t.Fatalf("round %d: publish returned %d", round, pubCode)
		}
		// Invariant: B is published, so the file row and the file on disk
		// must both exist.
		var fileRows int
		env.db.Get(&fileRows, `SELECT count(*) FROM files WHERE sha256=$1`, sha)
		if fileRows != 1 {
			t.Fatalf("round %d: files rows = %d, want 1", round, fileRows)
		}
		if _, err := os.Stat(env.fs.FilePath(sha)); err != nil {
			t.Fatalf("round %d: referenced file missing on disk: %v", round, err)
		}
		env.mustStatus(env.req(http.MethodDelete, fmt.Sprintf("/datasets/%d", dsB.ID), alice, nil, nil), http.StatusNoContent)
	}
}
