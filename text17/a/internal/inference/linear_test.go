package inference

import (
	"math"
	"testing"
)

func TestPredictBatchNumeric(t *testing.T) {
	m := &LinearModel{
		Labels:  []string{"neg", "pos"},
		Weights: [][]float64{{1, 0}, {0, 1}},
		Bias:    []float64{0, 0},
	}
	preds, err := m.PredictBatch([][]float64{{2, 1}, {0, 0}})
	if err != nil {
		t.Fatalf("PredictBatch: %v", err)
	}
	if len(preds) != 2 {
		t.Fatalf("got %d predictions, want 2", len(preds))
	}
	// logits [2,1] -> softmax [e^2/(e^2+e), e/(e^2+e)]
	e2, e1 := math.Exp(2.0), math.Exp(1.0)
	want0 := e2 / (e2 + e1)
	if math.Abs(preds[0].Probabilities[0]-want0) > 1e-12 {
		t.Errorf("p0 = %v, want %v", preds[0].Probabilities[0], want0)
	}
	if preds[0].Label != "neg" || preds[0].Index != 0 {
		t.Errorf("prediction = %+v, want label neg index 0", preds[0])
	}
	// logits [0,0] -> uniform
	if math.Abs(preds[1].Probabilities[0]-0.5) > 1e-12 {
		t.Errorf("uniform p0 = %v, want 0.5", preds[1].Probabilities[0])
	}
	if preds[1].Label != "neg" {
		t.Errorf("tie should pick first label, got %q", preds[1].Label)
	}
}

func TestPredictBatchWithBias(t *testing.T) {
	m := &LinearModel{
		Labels:  []string{"a", "b"},
		Weights: [][]float64{{0, 0}, {0, 0}},
		Bias:    []float64{1, -1},
	}
	preds, err := m.PredictBatch([][]float64{{5, 5}})
	if err != nil {
		t.Fatalf("PredictBatch: %v", err)
	}
	want := math.Exp(1) / (math.Exp(1) + math.Exp(-1))
	if math.Abs(preds[0].Probabilities[0]-want) > 1e-12 {
		t.Errorf("p = %v, want %v", preds[0].Probabilities[0], want)
	}
	if preds[0].Label != "a" {
		t.Errorf("label = %q, want a", preds[0].Label)
	}
}

func TestSoftmaxNumericallyStable(t *testing.T) {
	m := &LinearModel{
		Labels:  []string{"a", "b"},
		Weights: [][]float64{{1000, 0}, {1001, 0}},
		Bias:    []float64{0, 0},
	}
	preds, err := m.PredictBatch([][]float64{{1, 0}})
	if err != nil {
		t.Fatalf("PredictBatch: %v", err)
	}
	for i, p := range preds[0].Probabilities {
		if math.IsNaN(p) || math.IsInf(p, 0) {
			t.Fatalf("probability %d is not finite: %v", i, p)
		}
	}
	sum := preds[0].Probabilities[0] + preds[0].Probabilities[1]
	if math.Abs(sum-1) > 1e-12 {
		t.Errorf("probabilities sum to %v, want 1", sum)
	}
	if preds[0].Label != "b" {
		t.Errorf("label = %q, want b", preds[0].Label)
	}
}

func TestValidateRejectsBadShapes(t *testing.T) {
	cases := map[string]*LinearModel{
		"no labels":       {Labels: nil, Weights: [][]float64{{1}}, Bias: []float64{0}},
		"duplicate label": {Labels: []string{"a", "a"}, Weights: [][]float64{{1}, {2}}, Bias: []float64{0, 0}},
		"weight rows":     {Labels: []string{"a", "b"}, Weights: [][]float64{{1}}, Bias: []float64{0, 0}},
		"bias length":     {Labels: []string{"a"}, Weights: [][]float64{{1}}, Bias: []float64{0, 0}},
		"ragged weights":  {Labels: []string{"a", "b"}, Weights: [][]float64{{1, 2}, {1}}, Bias: []float64{0, 0}},
		"zero dim":        {Labels: []string{"a"}, Weights: [][]float64{{}}, Bias: []float64{0}},
		"nan weight":      {Labels: []string{"a"}, Weights: [][]float64{{math.NaN()}}, Bias: []float64{0}},
		"inf weight":      {Labels: []string{"a"}, Weights: [][]float64{{math.Inf(1)}}, Bias: []float64{0}},
		"nan bias":        {Labels: []string{"a"}, Weights: [][]float64{{1}}, Bias: []float64{math.NaN()}},
	}
	for name, m := range cases {
		if err := m.Validate(); err == nil {
			t.Errorf("%s: expected validation error", name)
		}
	}
}

func TestPredictBatchRejectsBadInputs(t *testing.T) {
	m := &LinearModel{
		Labels:  []string{"a", "b"},
		Weights: [][]float64{{1, 0}, {0, 1}},
		Bias:    []float64{0, 0},
	}
	if _, err := m.PredictBatch(nil); err == nil {
		t.Error("empty batch: expected error")
	}
	if _, err := m.PredictBatch([][]float64{{1, 2, 3}}); err == nil {
		t.Error("wrong row width: expected error")
	}
	if _, err := m.PredictBatch([][]float64{{math.NaN(), 0}}); err == nil {
		t.Error("NaN input: expected error")
	}
	if _, err := m.PredictBatch([][]float64{{math.Inf(-1), 0}}); err == nil {
		t.Error("-Inf input: expected error")
	}
}
