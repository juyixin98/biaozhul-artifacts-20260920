package inference

import (
	"math"
	"testing"
)

func TestEncodeDecodeRoundTrip(t *testing.T) {
	w := &Weights{
		InputDim:   3,
		NumClasses: 2,
		W: []float32{
			1, -1, 0.5,
			0, 1, -0.5,
		},
		B: []float32{-0.25, 0.25},
	}
	got, err := Decode(w.Encode())
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.InputDim != 3 || got.NumClasses != 2 {
		t.Fatalf("shape = %dx%d", got.InputDim, got.NumClasses)
	}
	for i := range w.W {
		if got.W[i] != w.W[i] {
			t.Fatalf("W[%d] = %v want %v", i, got.W[i], w.W[i])
		}
	}
	for i := range w.B {
		if got.B[i] != w.B[i] {
			t.Fatalf("B[%d] = %v want %v", i, got.B[i], w.B[i])
		}
	}
}

func TestDecodeRejectsBadBlobs(t *testing.T) {
	cases := map[string][]byte{
		"too short":       {1, 2, 3},
		"zero input dim":  {0, 0, 0, 0, 2, 0, 0, 0},
		"one class":       {1, 0, 0, 0, 1, 0, 0, 0},
		"length mismatch": append([]byte{1, 0, 0, 0, 2, 0, 0, 0}, make([]byte, 4)...),
	}
	for name, b := range cases {
		if _, err := Decode(b); err == nil {
			t.Fatalf("%s: expected error", name)
		}
	}

	// Non-finite weight must be rejected.
	w := &Weights{InputDim: 2, NumClasses: 2,
		W: []float32{float32(math.NaN()), 0, 0, 0}, B: []float32{0, 0}}
	if _, err := Decode(w.Encode()); err == nil {
		t.Fatal("expected non-finite W rejection")
	}
	w = &Weights{InputDim: 2, NumClasses: 2,
		W: []float32{0, 0, 0, 0}, B: []float32{float32(math.Inf(1)), 0}}
	if _, err := Decode(w.Encode()); err == nil {
		t.Fatal("expected non-finite bias rejection")
	}
}

func TestPredictMatchesHandComputedSoftmax(t *testing.T) {
	// z0 = x0 - x1 + 0.5 x2 - 0.25 ; z1 = x1 - 0.5 x2 + 0.25
	w := &Weights{
		InputDim:   3,
		NumClasses: 2,
		W: []float32{
			1, -1, 0.5,
			0, 1, -0.5,
		},
		B: []float32{-0.25, 0.25},
	}

	// x = [2,1,0]: z0 = 0.75, z1 = 1.25 -> class 1, sigmoid(0.5)=0.6224593
	_, probs, idx, err := w.Predict([]float32{2, 1, 0})
	if err != nil {
		t.Fatal(err)
	}
	if idx != 1 {
		t.Fatalf("idx = %d want 1", idx)
	}
	want1 := 1.0 / (1.0 + math.Exp(-0.5))
	if math.Abs(float64(probs[1])-want1) > 1e-6 {
		t.Fatalf("p1 = %v want %v", probs[1], want1)
	}
	if math.Abs(float64(probs[0]+probs[1])-1.0) > 1e-6 {
		t.Fatalf("probs do not sum to 1: %v", probs)
	}

	// x = [0,0,4]: z0 = 1.75, z1 = -1.75 -> class 0, p ~= 0.97069
	_, probs, idx, _ = w.Predict([]float32{0, 0, 4})
	if idx != 0 {
		t.Fatalf("idx = %d want 0", idx)
	}
	if math.Abs(float64(probs[0])-0.9706877692) > 1e-6 {
		t.Fatalf("p0 = %v", probs[0])
	}
}

func TestPredictIsDeterministicAndNotConstant(t *testing.T) {
	w := &Weights{InputDim: 2, NumClasses: 3,
		W: []float32{
			3, 0,
			0, 3,
			-3, -3,
		},
		B: []float32{0, 0, 0},
	}
	// Different inputs must drive different classes (no fixed prediction).
	inputs := [][]float32{{10, 0}, {0, 10}, {-10, -10}}
	wantIdx := []int{0, 1, 2}
	seen := map[int]bool{}
	for i, x := range inputs {
		_, p1, idx, err := w.Predict(x)
		if err != nil {
			t.Fatal(err)
		}
		if idx != wantIdx[i] {
			t.Fatalf("input %v: idx %d want %d", x, idx, wantIdx[i])
		}
		// Determinism.
		_, p2, idx2, _ := w.Predict(x)
		if idx != idx2 || p1[0] != p2[0] {
			t.Fatal("prediction not deterministic")
		}
		seen[idx] = true
		if p1[idx] < 0.99 {
			t.Fatalf("confident case p=%v", p1)
		}
	}
	if len(seen) != 3 {
		t.Fatal("model returns a constant prediction")
	}
}

func TestPredictRejectsBadShapeAndNonFinite(t *testing.T) {
	w := &Weights{InputDim: 2, NumClasses: 2,
		W: []float32{1, 0, 0, 1}, B: []float32{0, 0}}
	if _, _, _, err := w.Predict([]float32{1}); err == nil {
		t.Fatal("expected shape error")
	}
	if _, _, _, err := w.Predict([]float32{1, float32(math.NaN())}); err == nil {
		t.Fatal("expected NaN input rejection")
	}
	if _, _, _, err := w.Predict([]float32{1, float32(math.Inf(-1))}); err == nil {
		t.Fatal("expected Inf input rejection")
	}
}
