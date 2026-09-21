// Package nn implements the actual CPU forward pass of the only supported
// model: a single linear layer followed by softmax. There are no fixed
// predictions anywhere — every probability comes from the registered
// weights applied to the supplied input.
package nn

import (
	"fmt"
	"math"

	"github.com/synapticgo/synapticgo/internal/spec"
)

// LinearSoftmax is a ready-to-run model.
type LinearSoftmax struct {
	Dim     int
	Classes []string
	W       [][]float64 // [k][dim]
	B       []float64   // [k]
}

// FromWeights compiles validated weights into an inference-time model.
func FromWeights(w *spec.Weights) *LinearSoftmax {
	return &LinearSoftmax{Dim: w.Dim, Classes: w.Classes, W: w.W, B: w.B}
}

// K returns the number of classes.
func (m *LinearSoftmax) K() int { return len(m.Classes) }

// Forward computes logits[k] = W[k]·x + b[k]. Input shape and finiteness are
// validated. Multiplication and accumulation run in float64.
func (m *LinearSoftmax) Forward(x []float64) ([]float64, error) {
	if len(x) != m.Dim {
		return nil, fmt.Errorf("input has %d features, model expects %d", len(x), m.Dim)
	}
	for j, v := range x {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, fmt.Errorf("input feature %d is NaN or Inf", j)
		}
	}
	logits := make([]float64, m.K())
	for k := 0; k < m.K(); k++ {
		row := m.W[k]
		acc := m.B[k]
		for j := 0; j < m.Dim; j++ {
			acc += row[j] * x[j]
		}
		if math.IsNaN(acc) || math.IsInf(acc, 0) {
			return nil, fmt.Errorf("logit %d overflowed or became NaN", k)
		}
		logits[k] = acc
	}
	return logits, nil
}

// Predict runs the forward pass and a numerically stable softmax. It returns
// the class probabilities (sum 1 within floating-point tolerance), the
// predicted class index and its name.
func (m *LinearSoftmax) Predict(x []float64) (probs []float64, classIdx int, className string, err error) {
	logits, err := m.Forward(x)
	if err != nil {
		return nil, 0, "", err
	}
	probs = Softmax(logits)
	best := 0
	for k := 1; k < len(probs); k++ {
		if probs[k] > probs[best] {
			best = k
		}
	}
	return probs, best, m.Classes[best], nil
}

// Softmax returns exp(z - max z) / sum, the shift avoids overflow.
func Softmax(z []float64) []float64 {
	if len(z) == 0 {
		return z
	}
	maxZ := z[0]
	for _, v := range z[1:] {
		if v > maxZ {
			maxZ = v
		}
	}
	out := make([]float64, len(z))
	sum := 0.0
	for i, v := range z {
		out[i] = math.Exp(v - maxZ)
		sum += out[i]
	}
	if !(sum > 0) { // sum can only be 0/NaN under pathological weights; guard it
		uniform := 1.0 / float64(len(z))
		for i := range out {
			out[i] = uniform
		}
		return out
	}
	for i := range out {
		out[i] /= sum
	}
	return out
}
