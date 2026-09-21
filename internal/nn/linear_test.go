package nn_test

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/synapticgo/synapticgo/internal/nn"
	"github.com/synapticgo/synapticgo/internal/spec"
)

// Reference model:
//
//	2 inputs, 3 classes
//	W = [[1, 0], [0, 1], [1, 1]], b = [0, -1, 1]
//
// For x = [1, 2]:
//
//	logits = [1, 1, 4]
//	softmax = [e^1, e^1, e^4] / (2e + e^4)
func refModel(t *testing.T) *nn.LinearSoftmax {
	t.Helper()
	raw := []byte(`{
	  "type":"linear_softmax","version":1,"dim":2,
	  "classes":["a","b","c"],
	  "W":[[1,0],[0,1],[1,1]],
	  "b":[0,-1,1]
	}`)
	w, err := spec.ParseWeights(raw)
	if err != nil {
		t.Fatalf("parse weights: %v", err)
	}
	return nn.FromWeights(w)
}

func approx(a, b, tol float64) bool { return math.Abs(a-b) <= tol }

func TestPredictReferenceValues(t *testing.T) {
	m := refModel(t)
	probs, idx, name, err := m.Predict([]float64{1, 2})
	if err != nil {
		t.Fatalf("predict: %v", err)
	}
	if idx != 2 || name != "c" {
		t.Fatalf("want class 2/c, got %d/%q", idx, name)
	}
	if len(probs) != 3 {
		t.Fatalf("want 3 probs, got %d", len(probs))
	}
	var sum float64
	for _, p := range probs {
		sum += p
	}
	if !approx(sum, 1.0, 1e-12) {
		t.Fatalf("probabilities sum to %.15f", sum)
	}
	// Hand-computed softmax.
	e1 := math.Exp(1 - 4)
	e2 := math.Exp(1 - 4)
	e3 := 1.0
	den := e1 + e2 + e3
	want := []float64{e1 / den, e2 / den, e3 / den}
	for i := range want {
		if !approx(probs[i], want[i], 1e-14) {
			t.Fatalf("prob[%d]=%.15f want %.15f", i, probs[i], want[i])
		}
	}
}

func TestPredictIsDeterministicAndDataDependent(t *testing.T) {
	m := refModel(t)
	p1, _, _, _ := m.Predict([]float64{1, 2})
	p2, _, _, _ := m.Predict([]float64{1, 2})
	for i := range p1 {
		if p1[i] != p2[i] {
			t.Fatalf("nondeterministic prediction: %v vs %v", p1, p2)
		}
	}
	// Different inputs must produce different probabilities (not a constant).
	p3, idx3, _, _ := m.Predict([]float64{-5, -5})
	same := true
	for i := range p1 {
		if p1[i] != p3[i] {
			same = false
		}
	}
	if same {
		t.Fatal("predictor returns identical output for different inputs")
	}
	if idx3 == 2 {
		// With x=[-5,-5], logits = [-5,-6,-9]; class 0 must win, not class 2.
		t.Fatalf("unexpected argmax for [-5,-5]: %d", idx3)
	}
}

func TestSoftmaxStableForLargeLogits(t *testing.T) {
	probs := nn.Softmax([]float64{1000, 1001, 1002})
	for _, p := range probs {
		if math.IsNaN(p) || math.IsInf(p, 0) {
			t.Fatalf("non-finite softmax: %v", probs)
		}
	}
	var sum float64
	for _, p := range probs {
		sum += p
	}
	if !approx(sum, 1, 1e-12) {
		t.Fatalf("sum=%v", sum)
	}
}

func TestForwardShapeAndFiniteChecks(t *testing.T) {
	m := refModel(t)
	if _, err := m.Forward([]float64{1}); err == nil {
		t.Fatal("expected shape error for 1-d input")
	}
	if _, err := m.Forward([]float64{math.NaN(), 1}); err == nil {
		t.Fatal("expected NaN rejection")
	}
	if _, err := m.Forward([]float64{1, math.Inf(1)}); err == nil {
		t.Fatal("expected +Inf rejection")
	}
}

func TestCanonicalWeightsHashStable(t *testing.T) {
	// Field order/whitespace changes must not change the canonical hash.
	a := []byte(`{"type":"linear_softmax","version":1,"dim":1,"classes":["x","y"],"W":[[1],[2]],"b":[0,0]}`)
	var reordered map[string]any
	if err := json.Unmarshal(a, &reordered); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(reordered)

	wa, err := spec.ParseWeights(a)
	if err != nil {
		t.Fatal(err)
	}
	wb, err := spec.ParseWeights(b)
	if err != nil {
		t.Fatal(err)
	}
	_, ha, err := spec.CanonicalWeights(wa)
	if err != nil {
		t.Fatal(err)
	}
	_, hb, err := spec.CanonicalWeights(wb)
	if err != nil {
		t.Fatal(err)
	}
	if ha != hb {
		t.Fatalf("canonical hash changed with field ordering: %s vs %s", ha, hb)
	}
}

func TestValidateWeightsRejectsBadShapes(t *testing.T) {
	cases := [][]byte{
		[]byte(`{"type":"mlp","version":1,"dim":1,"classes":["x","y"],"W":[[1],[2]],"b":[0,0]}`),
		[]byte(`{"type":"linear_softmax","version":1,"dim":2,"classes":["x","y"],"W":[[1],[2]],"b":[0,0]}`),
		[]byte(`{"type":"linear_softmax","version":1,"dim":1,"classes":["x","y"],"W":[[1],[2]],"b":[0]}`),
		[]byte(`{"type":"linear_softmax","version":1,"dim":1,"classes":["x","x"],"W":[[1],[2]],"b":[0,0]}`),
		[]byte(`{"type":"linear_softmax","version":1,"dim":1,"classes":["x","y"],"W":[[1],[2]],"b":[0,"NaN"]}`),
	}
	for i, raw := range cases {
		if _, err := spec.ParseWeights(raw); err == nil {
			t.Fatalf("case %d: expected validation error", i)
		}
	}
}

func TestParseDatasetValidation(t *testing.T) {
	good := []byte(`{"dim":2,"examples":[{"x":[1,2],"label":0},{"x":[3,4]}]}`)
	if _, err := spec.ParseDataset(good); err != nil {
		t.Fatalf("good dataset rejected: %v", err)
	}
	bad := [][]byte{
		[]byte(`{"dim":2,"examples":[{"x":[1],"label":0}]}`),
		[]byte(`{"dim":2,"examples":[]}`),
		[]byte(`{"dim":0,"examples":[]}`),
		[]byte(`{"dim":2,"examples":[{"x":[1,2],"label":-1}]}`),
	}
	for i, raw := range bad {
		if _, err := spec.ParseDataset(raw); err == nil {
			t.Fatalf("bad case %d accepted", i)
		}
	}
}
