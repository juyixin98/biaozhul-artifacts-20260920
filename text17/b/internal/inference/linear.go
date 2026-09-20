// Package inference implements the CPU forward pass for the only supported
// model: a single-layer linear classifier (multinomial logistic regression),
// logits = x·Wᵀ + b followed by a numerically stable softmax.
package inference

import (
	"fmt"
	"math"
)

// LinearModel is the one-layer softmax classifier.
type LinearModel struct {
	Labels  []string    // class names, index aligns with Weights rows
	Weights [][]float64 // [numClasses][inputDim]
	Bias    []float64   // [numClasses]
}

// NumClasses returns the number of output classes.
func (m *LinearModel) NumClasses() int { return len(m.Labels) }

// InputDim returns the required feature width.
func (m *LinearModel) InputDim() int {
	if len(m.Weights) == 0 {
		return 0
	}
	return len(m.Weights[0])
}

// Validate checks label/weight/bias tensor shapes and rejects NaN/Inf
// parameters so a registered model can always produce finite output.
func (m *LinearModel) Validate() error {
	if len(m.Labels) == 0 {
		return fmt.Errorf("labels must not be empty")
	}
	seen := make(map[string]bool, len(m.Labels))
	for _, l := range m.Labels {
		if l == "" {
			return fmt.Errorf("labels must not contain empty strings")
		}
		if seen[l] {
			return fmt.Errorf("duplicate label %q", l)
		}
		seen[l] = true
	}
	if len(m.Weights) != len(m.Labels) {
		return fmt.Errorf("weights rows (%d) must match number of labels (%d)",
			len(m.Weights), len(m.Labels))
	}
	if len(m.Bias) != len(m.Labels) {
		return fmt.Errorf("bias length (%d) must match number of labels (%d)",
			len(m.Bias), len(m.Labels))
	}
	dim := m.InputDim()
	if dim == 0 {
		return fmt.Errorf("weights must have at least one column")
	}
	for j, row := range m.Weights {
		if len(row) != dim {
			return fmt.Errorf("weights row %d has length %d, want %d", j, len(row), dim)
		}
		for i, v := range row {
			if !finite(v) {
				return fmt.Errorf("weights[%d][%d] is not finite", j, i)
			}
		}
	}
	for j, v := range m.Bias {
		if !finite(v) {
			return fmt.Errorf("bias[%d] is not finite", j)
		}
	}
	return nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// Prediction is the softmax result for one input row.
type Prediction struct {
	Index         int       `json:"index"`
	Label         string    `json:"label"`
	Probabilities []float64 `json:"probabilities"`
}

// PredictBatch runs the real forward pass over a batch of feature rows. It
// validates each row's width and finiteness; results are computed from the
// stored weights, never from a fixed answer.
func (m *LinearModel) PredictBatch(inputs [][]float64) ([]Prediction, error) {
	if len(inputs) == 0 {
		return nil, fmt.Errorf("inputs must contain at least one row")
	}
	dim := m.InputDim()
	for r, row := range inputs {
		if len(row) != dim {
			return nil, fmt.Errorf("input row %d has length %d, want %d", r, len(row), dim)
		}
		for i, v := range row {
			if !finite(v) {
				return nil, fmt.Errorf("input[%d][%d] is not finite", r, i)
			}
		}
	}

	out := make([]Prediction, len(inputs))
	for r, x := range inputs {
		logits := make([]float64, m.NumClasses())
		for j := 0; j < m.NumClasses(); j++ {
			z := m.Bias[j]
			w := m.Weights[j]
			for i, xv := range x {
				z += xv * w[i]
			}
			logits[j] = z
		}
		probs := softmax(logits)
		best := 0
		for j := 1; j < len(probs); j++ {
			if probs[j] > probs[best] {
				best = j
			}
		}
		out[r] = Prediction{Index: best, Label: m.Labels[best], Probabilities: probs}
	}
	return out, nil
}

// softmax returns exp(z)/sum(exp(z)) in a numerically stable way by
// subtracting the largest logit before exponentiating.
func softmax(logits []float64) []float64 {
	max := logits[0]
	for _, v := range logits[1:] {
		if v > max {
			max = v
		}
	}
	sum := 0.0
	out := make([]float64, len(logits))
	for j, v := range logits {
		out[j] = math.Exp(v - max)
		sum += out[j]
	}
	inv := 1.0 / sum
	for j := range out {
		out[j] *= inv
	}
	return out
}
