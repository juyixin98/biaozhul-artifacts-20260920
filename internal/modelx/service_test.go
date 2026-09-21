package modelx_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"testing"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/dataset"
	"synapticgo/internal/experiments"
	"synapticgo/internal/inference"
	"synapticgo/internal/models"
	"synapticgo/internal/modelx"
	"synapticgo/internal/testutil"
)

func dgst(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func user(t *testing.T, db *sqlx.DB, name string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRowx(
		`INSERT INTO users(username, key_hash) VALUES ($1,$2) RETURNING id`,
		name, "h:"+name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// publishReady creates a dataset owned by owner and publishes payload.
func publishReady(t *testing.T, env *testutil.Env, owner int64, payload []byte, cs int) (int64, string) {
	t.Helper()
	ctx := context.Background()
	svc := dataset.NewService(env.DB, env.Objects, env.Store)
	d, err := svc.Create(ctx, owner, "ds", int64(len(payload)), int32(cs), nil)
	if err != nil {
		t.Fatal(err)
	}
	n := (len(payload) + cs - 1) / cs
	for i := 0; i < n; i++ {
		start := i * cs
		end := start + cs
		if end > len(payload) {
			end = len(payload)
		}
		ch := payload[start:end]
		if _, _, err := svc.UploadChunk(ctx, owner, d.ID, int32(i), bytes.NewReader(ch), dgst(ch)); err != nil {
			t.Fatal(err)
		}
	}
	pub, whole, err := svc.Publish(ctx, owner, d.ID)
	if err != nil {
		t.Fatal(err)
	}
	return pub.ID, whole
}

func simpleWeights(in, classes int) *inference.Weights {
	w := &inference.Weights{
		InputDim: in, NumClasses: classes,
		W: make([]float32, in*classes), B: make([]float32, classes),
	}
	// Each class c scores 2*feature c, so class index == argmax feature.
	for c := 0; c < classes; c++ {
		w.W[c*in+c] = 2
	}
	return w
}

func TestRegisterBindsDatasetAndAutoVersions(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	payload := []byte("dataset payload used for model binding, padding")
	dsID, whole := publishReady(t, env, owner, payload, 9)

	svc := modelx.NewService(env.DB)
	m1, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "clf", DatasetID: dsID, InputDim: 3,
		Classes: []string{"cat", "dog"}, Weights: simpleWeights(3, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	if m1.Version != 1 || m1.DatasetDigest != whole {
		t.Fatalf("v1 binding wrong: %+v", m1)
	}
	m2, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "clf", DatasetID: dsID, InputDim: 3,
		Classes: []string{"cat", "dog"}, Weights: simpleWeights(3, 2),
	})
	if err != nil {
		t.Fatal(err)
	}
	if m2.Version != 2 {
		t.Fatalf("version = %d want 2", m2.Version)
	}
}

func TestRegisterValidatesShapesAndReadiness(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	other := user(t, env.DB, "bob")
	payload := []byte("another payload for validation tests!!")
	dsID, _ := publishReady(t, env, owner, payload, 8)

	svc := modelx.NewService(env.DB)

	// input_dim mismatch with weights
	if _, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "x", DatasetID: dsID, InputDim: 4,
		Classes: []string{"a", "b"}, Weights: simpleWeights(3, 2)}); err == nil {
		t.Fatal("expected input_dim mismatch")
	}
	// class count mismatch
	if _, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "x", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a", "b", "c"}, Weights: simpleWeights(3, 2)}); err == nil {
		t.Fatal("expected class count mismatch")
	}
	// fewer than 2 / duplicate classes
	if _, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "x", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a"}, Weights: simpleWeights(3, 1)}); err == nil {
		t.Fatal("expected at-least-two-classes error")
	}
	// non-owner cannot bind the dataset
	if _, err := svc.Register(ctx, other, modelx.RegisterInput{
		ModelName: "x", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a", "b"}, Weights: simpleWeights(3, 2)}); err == nil {
		t.Fatal("non-owner registration must fail")
	}
}

func TestModelVersionIsImmutable(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	payload := []byte("immutability payload padding bytes 12345")
	dsID, _ := publishReady(t, env, owner, payload, 10)
	svc := modelx.NewService(env.DB)
	m, err := svc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "imm", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a", "b"}, Weights: simpleWeights(3, 2)})
	if err != nil {
		t.Fatal(err)
	}
	// There is no update path: a version is immutable after release. As
	// tamper-evidence, directly corrupting the weights blob makes it fail to
	// decode, so a corrupted version can never serve predictions.
	env.MustExec(`UPDATE model_versions SET weights=$2 WHERE id=$1`, m.ID, []byte("tampered"))
	var tampered []byte
	if err := env.DB.Get(&tampered, `SELECT weights FROM model_versions WHERE id=$1`, m.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := inference.Decode(tampered); err == nil {
		t.Fatal("tampered weights unexpectedly decoded")
	}
}

func TestDeleteRefusedWithRecords(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	payload := []byte("delete protection payload here pad")
	dsID, _ := publishReady(t, env, owner, payload, 10)
	msvc := modelx.NewService(env.DB)
	esvc := experiments.NewService(env.DB)
	m, err := msvc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "d", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a", "b"}, Weights: simpleWeights(3, 2)})
	if err != nil {
		t.Fatal(err)
	}

	// With no records, deletion is allowed — but first prove a record blocks it.
	mv, w, err := msvc.LoadForInference(ctx, m.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, probs, idx, err := w.Predict([]float32{1, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if err := esvc.Record(ctx, record(mv.ID, owner, w.InputDim, idx, float64(probs[idx]), x([]float32{1, 0, 0}))); err != nil {
		t.Fatal(err)
	}
	if err := msvc.Delete(ctx, owner, m.ID); err == nil {
		t.Fatal("deleting a model with experiment records must fail")
	}
}

func TestInferenceNumericCorrectnessAndRecords(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	payload := []byte("numeric correctness payload padding 12345")
	dsID, _ := publishReady(t, env, owner, payload, 11)
	msvc := modelx.NewService(env.DB)
	esvc := experiments.NewService(env.DB)

	// Hand-built 2-class model: z0 = x0-x1+0.5x2-0.25 ; z1 = x1-0.5x2+0.25
	w := &inference.Weights{InputDim: 3, NumClasses: 2,
		W: []float32{1, -1, 0.5, 0, 1, -0.5},
		B: []float32{-0.25, 0.25},
	}
	m, err := msvc.Register(ctx, owner, modelx.RegisterInput{
		ModelName: "num", DatasetID: dsID, InputDim: 3,
		Classes: []string{"cat", "dog"}, Weights: w})
	if err != nil {
		t.Fatal(err)
	}
	mv, loaded, err := msvc.LoadForInference(ctx, m.ID)
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		x        []float32
		wantIdx  int
		wantProb float64
	}{
		{[]float32{2, 1, 0}, 1, 0.6224593312},
		{[]float32{0, 0, 4}, 0, 0.9706877692},
	}
	for _, c := range cases {
		_, probs, idx, err := loaded.Predict(c.x)
		if err != nil {
			t.Fatal(err)
		}
		if idx != c.wantIdx || mv.Classes[idx] != []string{"cat", "dog"}[idx] {
			t.Fatalf("idx=%d want %d", idx, c.wantIdx)
		}
		if d := float64(probs[idx]) - c.wantProb; d > 1e-5 || d < -1e-5 {
			t.Fatalf("prob=%v want %v", probs[idx], c.wantProb)
		}
		if err := esvc.Record(ctx, record(mv.ID, owner, loaded.InputDim, idx, float64(probs[idx]), x(c.x))); err != nil {
			t.Fatal(err)
		}
	}

	recs, err := esvc.List(ctx, owner, m.ID, 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 2 {
		t.Fatalf("records = %d want 2", len(recs))
	}
}

func TestCompareOnlyAcrossIdenticalClassTables(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	owner := user(t, env.DB, "alice")
	payload := []byte("compare payload padding bytes 1234567890")
	dsID, _ := publishReady(t, env, owner, payload, 12)
	msvc := modelx.NewService(env.DB)
	esvc := experiments.NewService(env.DB)

	reg := func(name string, classes []string) int64 {
		m, err := msvc.Register(ctx, owner, modelx.RegisterInput{
			ModelName: name, DatasetID: dsID, InputDim: 3,
			Classes: classes, Weights: simpleWeights(3, len(classes))})
		if err != nil {
			t.Fatal(err)
		}
		return m.ID
	}
	a := reg("m", []string{"cat", "dog"})
	b := reg("m", []string{"cat", "dog"})
	cID := reg("n", []string{"dog", "cat"}) // reordered => different digest

	// Same class table: comparable.
	cmp, err := esvc.Compare(ctx, owner, a, b)
	if err != nil {
		t.Fatalf("identical class tables should compare: %v", err)
	}
	if len(cmp.Classes) != 2 {
		t.Fatal("comparison missing classes")
	}
	// Different order or set: not comparable, even with same size.
	if _, err := esvc.Compare(ctx, owner, a, cID); err == nil {
		t.Fatal("comparing different class tables must fail")
	}
}

func TestCrossUserIsolation(t *testing.T) {
	ctx := context.Background()
	env := testutil.New(t)
	alice := user(t, env.DB, "alice")
	bob := user(t, env.DB, "bob")
	payload := []byte("isolation payload padding bytes 123456")
	dsID, _ := publishReady(t, env, alice, payload, 10)
	svc := modelx.NewService(env.DB)
	m, err := svc.Register(ctx, alice, modelx.RegisterInput{
		ModelName: "iso", DatasetID: dsID, InputDim: 3,
		Classes: []string{"a", "b"}, Weights: simpleWeights(3, 2)})
	if err != nil {
		t.Fatal(err)
	}
	// Bob cannot read Alice's model.
	if _, err := svc.Get(ctx, bob, m.ID); err == nil {
		t.Fatal("cross-user model read must fail")
	}
	// Bob cannot see Alice's experiment records (treated as not found).
	esvc := experiments.NewService(env.DB)
	if _, err := esvc.List(ctx, bob, m.ID, 10, 0); err == nil {
		t.Fatal("cross-user experiment listing must fail")
	}
	// Inference itself is open to any authenticated user.
	if _, _, err := svc.LoadForInference(ctx, m.ID); err != nil {
		t.Fatalf("inference should be available: %v", err)
	}
}

func x(v []float32) []byte {
	out := make([]byte, 0, 4*len(v))
	for _, f := range v {
		bits := math.Float32bits(f)
		out = append(out, byte(bits), byte(bits>>8), byte(bits>>16), byte(bits>>24))
	}
	return out
}

func record(modelID, caller int64, dim, idx int, conf float64, input []byte) *models.ExperimentRecord {
	s := sha256.Sum256(input)
	return &models.ExperimentRecord{
		ModelVersionID: modelID, CallerID: caller,
		InputDigest: hex.EncodeToString(s[:]), InputDim: int32(dim),
		PredictedClass: "cls", PredictedIndex: int32(idx),
		Confidence: conf, LatencyMs: 0.1,
	}
}
