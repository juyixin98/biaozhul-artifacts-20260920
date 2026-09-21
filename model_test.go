package synapticgo_test

import (
	"encoding/json"
	"fmt"
	"math"
	"testing"
)

func predict(t *testing.T, c *client, modelID int, x []float64) (int, map[string]any) {
	t.Helper()
	code, body := c.doJSON("POST", fmt.Sprintf("/v1/models/%d/predict", modelID),
		map[string]any{"x": x}, nil)
	if code != 200 {
		t.Fatalf("predict: %d %s", code, body)
	}
	var v map[string]any
	mustJSON(body, &v)
	return code, v
}

func TestInferenceNumericalCorrectness(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	// Register the exact reference model from nn tests:
	// W=[[1,0],[0,1],[1,1]], b=[0,-1,1], classes a,b,c, input x=[1,2].
	weights := map[string]any{
		"type": "linear_softmax", "version": 1, "dim": 2,
		"classes": []string{"a", "b", "c"},
		"W":       [][]float64{{1, 0}, {0, 1}, {1, 1}},
		"b":       []float64{0, -1, 1},
	}
	code, body := alice.doJSON("POST", "/v1/models",
		map[string]any{"name": "ref", "version": "v1", "weights": weights}, nil)
	if code != 201 {
		t.Fatalf("register: %d %s", code, body)
	}
	var mv map[string]any
	mustJSON(body, &mv)
	id := int(mv["id"].(float64))

	_, res := predict(t, alice, id, []float64{1, 2})
	if int(res["predicted_class"].(float64)) != 2 || res["class_name"] != "c" {
		t.Fatalf("wrong class: %v", res)
	}
	probsAny := res["probabilities"].([]any)
	probs := make([]float64, 3)
	var sum float64
	for i, p := range probsAny {
		probs[i] = p.(float64)
		sum += probs[i]
	}
	if math.Abs(sum-1) > 1e-12 {
		t.Fatalf("probabilities sum %.15f", sum)
	}
	// Expected softmax for logits [1,1,4] computed with shift 4.
	e1 := math.Exp(-3)
	want := []float64{e1 / (2*e1 + 1), e1 / (2*e1 + 1), 1 / (2*e1 + 1)}
	for i := range want {
		if math.Abs(probs[i]-want[i]) > 1e-14 {
			t.Fatalf("prob[%d]=%.15f want %.15f", i, probs[i], want[i])
		}
	}

	// Independent vector -> independently computed result (no constant
	// predictor): x=[-10,-10] yields logits [-10,-11,-19], class a wins.
	_, res2 := predict(t, alice, id, []float64{-10, -10})
	if int(res2["predicted_class"].(float64)) != 0 {
		t.Fatalf("expected class 0 for [-10,-10], got %v", res2["predicted_class"])
	}
}

func TestInferenceRejectsBadShapesAndNonFinite(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")
	mv := registerLinearModel(t, alice, "m", "v1", 2, []string{"x", "y"}, nil, nil, nil)
	id := int(mv["id"].(float64))

	for _, tc := range []struct {
		name string
		x    []float64
	}{
		{"too short", []float64{1}},
		{"too long", []float64{1, 2, 3}},
		{"empty", []float64{}},
	} {
		code, _ := alice.doJSON("POST", fmt.Sprintf("/v1/models/%d/predict", id),
			map[string]any{"x": tc.x}, nil)
		if code != 422 {
			t.Fatalf("%s: want 422, got %d", tc.name, code)
		}
	}
	// NaN/Inf cannot arrive via JSON numbers (decoding errors), but reject
	// malformed bodies outright.
	code, _ := alice.env.do("POST", fmt.Sprintf("/v1/models/%d/predict", id),
		alice.token, []byte(`{"x":[1,NaN]}`))
	if code != 400 {
		t.Fatalf("NaN JSON body: want 400, got %d", code)
	}
}

func TestModelVersionImmutabilityAndExperiments(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")
	mv := registerLinearModel(t, alice, "m", "v1", 2, []string{"x", "y"}, nil, nil, nil)
	id := int(mv["id"].(float64))

	predict(t, alice, id, []float64{1, 2})
	predict(t, alice, id, []float64{3, 4})

	code, body := alice.doJSON("GET", "/v1/experiments", nil, nil)
	if code != 200 {
		t.Fatalf("experiments: %d %s", code, body)
	}
	var logs []map[string]any
	mustJSON(body, &logs)
	if len(logs) != 2 {
		t.Fatalf("want 2 experiment rows, got %d", len(logs))
	}
	for _, l := range logs {
		if int(l["model_version_id"].(float64)) != id {
			t.Fatal("experiment linked to wrong model")
		}
		if l["probs"] == nil || l["input"] == nil {
			t.Fatal("experiment missing input/probs")
		}
	}

	// Filtering by model and pagination.
	code, body = alice.doJSON("GET", fmt.Sprintf("/v1/experiments?model_version_id=%d&limit=1", id), nil, nil)
	mustJSON(body, &logs)
	if code != 200 || len(logs) != 1 {
		t.Fatalf("filtered experiments: %d %v", code, logs)
	}
	older := logs[0]
	code, body = alice.doJSON("GET", fmt.Sprintf("/v1/experiments?before=%v", older["id"]), nil, nil)
	mustJSON(body, &logs)
	if len(logs) != 1 {
		t.Fatalf("pagination before: got %d rows", len(logs))
	}

	// Version can't be overwritten: re-register same name/version conflicts.
	dup := map[string]any{
		"name": "m", "version": "v1",
		"weights": map[string]any{
			"type": "linear_softmax", "version": 1, "dim": 2,
			"classes": []string{"x", "y"}, "W": [][]float64{{9, 9}, {9, 9}}, "b": []float64{0, 0},
		},
	}
	if code, b := alice.doJSON("POST", "/v1/models", dup, nil); code != 409 {
		t.Fatalf("re-register same version: want 409, got %d %s", code, b)
	}
	// Stored weights unchanged: prediction still reflects original weights.
	_, res := predict(t, alice, id, []float64{1, 2})
	probs := res["probabilities"].([]any)
	if probs[0].(float64) == probs[1].(float64) && probs[1].(float64) == probs[2].(float64) {
		t.Fatal("weights appear overwritten with constant predictor")
	}
}

func TestModelDatasetBindingAndComparison(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	train := datasetJSON2D()
	tid, _ := uploadAndPublish(t, alice, "train", train, nil)
	eid, _ := uploadAndPublish(t, alice, "eval", train, nil) // same content hash

	v1 := registerLinearModel(t, alice, "m", "v1", 2, []string{"zero", "one"},
		ptrInt64(int64(tid)), ptrInt64(int64(eid)),
		map[string]float64{"accuracy": 0.7, "log_loss": 0.6})
	v2 := registerLinearModel(t, alice, "m", "v2", 2, []string{"zero", "one"},
		nil, ptrInt64(int64(eid)),
		map[string]float64{"accuracy": 0.9, "log_loss": 0.3})

	// Comparison succeeds: same ordered classes, same eval dataset hash.
	code, body := alice.doJSON("GET",
		fmt.Sprintf("/v1/model-comparisons?a=%v&b=%v", v1["id"], v2["id"]), nil, nil)
	if code != 200 {
		t.Fatalf("compare: %d %s", code, body)
	}
	var cmp map[string]any
	mustJSON(body, &cmp)
	if cmp["comparable"] != true {
		t.Fatalf("expected comparable: %v", cmp["reason"])
	}
	metrics := cmp["metrics"].([]any)
	byName := map[string]map[string]any{}
	for _, m := range metrics {
		mm := m.(map[string]any)
		byName[mm["metric"].(string)] = mm
	}
	if d := byName["accuracy"]["delta_b_minus_a"].(float64); math.Abs(d-0.2) > 1e-12 {
		t.Fatalf("accuracy delta=%v want 0.2", d)
	}
	if d := byName["log_loss"]["delta_b_minus_a"].(float64); math.Abs(d+0.3) > 1e-12 {
		t.Fatalf("log_loss delta=%v want -0.3", d)
	}

	// Different class table -> 422 and explicitly not comparable.
	v3 := registerLinearModel(t, alice, "m", "v3", 2, []string{"one", "zero"},
		nil, nil, map[string]float64{"accuracy": 0.99})
	code, body = alice.doJSON("GET",
		fmt.Sprintf("/v1/model-comparisons?a=%v&b=%v", v1["id"], v3["id"]), nil, nil)
	if code != 422 {
		t.Fatalf("compare different class tables: want 422, got %d", code)
	}
	var errResp map[string]any
	_ = json.Unmarshal(body, &errResp)

	// Different eval datasets -> 422.
	other := []byte(`{"dim":2,"examples":[{"x":[2,2],"label":1},{"x":[3,3],"label":1}]}`)
	oid, _ := uploadAndPublish(t, alice, "eval2", other, nil)
	v4 := registerLinearModel(t, alice, "m", "v4", 2, []string{"zero", "one"},
		nil, ptrInt64(int64(oid)), map[string]float64{"accuracy": 0.95})
	code, _ = alice.doJSON("GET",
		fmt.Sprintf("/v1/model-comparisons?a=%v&b=%v", v1["id"], v4["id"]), nil, nil)
	if code != 422 {
		t.Fatalf("compare different eval sets: want 422, got %d", code)
	}

	// Different input dimension -> rejected at registration against dataset.
	bad := map[string]any{
		"name": "m2", "version": "v1",
		"weights": map[string]any{
			"type": "linear_softmax", "version": 1, "dim": 3,
			"classes": []string{"zero", "one"},
			"W":       [][]float64{{1, 2, 3}, {4, 5, 6}}, "b": []float64{0, 0},
		},
		"train_dataset_id": tid,
	}
	if code, b := alice.doJSON("POST", "/v1/models", bad, nil); code != 422 {
		t.Fatalf("dim mismatch binding: want 422, got %d %s", code, b)
	}
}

func TestRegisterRejectsInvalidWeightsAndAuth(t *testing.T) {
	env := newTestEnv(t)
	alice := env.registerUser("alice")

	bad := map[string]any{
		"name": "bad", "version": "v1",
		"weights": map[string]any{
			"type": "linear_softmax", "version": 1, "dim": 2,
			"classes": []string{"x", "y"},
			"W":       [][]float64{{1, 2}, {3, 4, 5}}, // ragged row
			"b":       []float64{0, 0},
		},
	}
	if code, _ := alice.doJSON("POST", "/v1/models", bad, nil); code != 422 {
		t.Fatalf("ragged weights: want 422, got %d", code)
	}
	// No token -> 401.
	if code, _ := env.do("GET", "/v1/me", "", nil); code != 401 {
		t.Fatalf("unauthenticated: want 401, got %d", code)
	}
	if code, _ := env.do("GET", "/v1/me", "Bearer sgk_wrong", nil); code != 401 {
		t.Fatalf("bad token: want 401, got %d", code)
	}
}
