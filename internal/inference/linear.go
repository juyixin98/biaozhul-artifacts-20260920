// Package inference implements the single supported model: a one-layer linear
// classifier y = softmax(W x + b). The forward pass runs on CPU in Go using
// float32; there are no fixed or stubbed predictions.
package inference

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// Weights packs the parameter matrix. W is row-major [numClasses][inputDim],
// B has length numClasses.
type Weights struct {
	InputDim   int
	NumClasses int
	W          []float32 // len = NumClasses * InputDim
	B          []float32 // len = NumClasses
}

// Encode serializes weights to the binary blob stored with a model version:
//
//	uint32 inputDim, uint32 numClasses, then W and B as little-endian float32.
func (w *Weights) Encode() []byte {
	buf := make([]byte, 8+4*len(w.W)+4*len(w.B))
	binary.LittleEndian.PutUint32(buf[0:4], uint32(w.InputDim))
	binary.LittleEndian.PutUint32(buf[4:8], uint32(w.NumClasses))
	off := 8
	for _, v := range w.W {
		binary.LittleEndian.PutUint32(buf[off:off+4], math.Float32bits(v))
		off += 4
	}
	for _, v := range w.B {
		binary.LittleEndian.PutUint32(buf[off:off+4], math.Float32bits(v))
		off += 4
	}
	return buf
}

// Decode parses an encoded weight blob, validating its exact length and
// finiteness of every coefficient.
func Decode(b []byte) (*Weights, error) {
	if len(b) < 8 {
		return nil, errors.New("weights: blob too short")
	}
	inputDim := int(binary.LittleEndian.Uint32(b[0:4]))
	numClasses := int(binary.LittleEndian.Uint32(b[4:8]))
	if inputDim <= 0 || numClasses < 2 {
		return nil, fmt.Errorf("weights: invalid shape inputDim=%d numClasses=%d", inputDim, numClasses)
	}
	need := 8 + 4*inputDim*numClasses + 4*numClasses
	if len(b) != need {
		return nil, fmt.Errorf("weights: blob length %d, expected %d for shape %dx%d (+bias)",
			len(b), need, numClasses, inputDim)
	}
	w := &Weights{InputDim: inputDim, NumClasses: numClasses,
		W: make([]float32, inputDim*numClasses), B: make([]float32, numClasses)}
	off := 8
	for i := range w.W {
		v := math.Float32frombits(binary.LittleEndian.Uint32(b[off : off+4]))
		if !isFinite(v) {
			return nil, fmt.Errorf("weights: non-finite W coefficient at %d", i)
		}
		w.W[i] = v
		off += 4
	}
	for i := range w.B {
		v := math.Float32frombits(binary.LittleEndian.Uint32(b[off : off+4]))
		if !isFinite(v) {
			return nil, fmt.Errorf("weights: non-finite bias at %d", i)
		}
		w.B[i] = v
		off += 4
	}
	return w, nil
}

// ErrShape is returned when the input vector does not match the model.
var ErrShape = errors.New("input vector shape mismatch")

// Predict runs z_c = b_c + sum_d W[c,d]*x[d] then a numerically stable softmax
// (subtract max before exponentiating). Ties resolve to the lowest class
// index, which is deterministic.
func (w *Weights) Predict(x []float32) (logits, probs []float32, argmax int, err error) {
	if len(x) != w.InputDim {
		return nil, nil, 0, fmt.Errorf("%w: got %d features, model expects %d",
			ErrShape, len(x), w.InputDim)
	}
	for i, v := range x {
		if !isFinite(v) {
			return nil, nil, 0, fmt.Errorf("input contains non-finite value at index %d", i)
		}
	}

	logits = make([]float32, w.NumClasses)
	var maxZ float32 = -math.MaxFloat32
	for c := 0; c < w.NumClasses; c++ {
		row := w.W[c*w.InputDim : (c+1)*w.InputDim]
		var sum float32
		// Small-block accumulation keeps the result honest without pulling in
		// a third-party BLAS dependency.
		for d := 0; d < w.InputDim; d++ {
			sum += row[d] * x[d]
		}
		z := sum + w.B[c]
		if !isFinite(z) {
			return nil, nil, 0, fmt.Errorf("logit for class %d is non-finite", c)
		}
		logits[c] = z
		if z > maxZ {
			maxZ = z
		}
	}

	probs = make([]float32, w.NumClasses)
	var denom float64
	exps := make([]float64, w.NumClasses)
	for c, z := range logits {
		e := math.Exp(float64(z - maxZ))
		exps[c] = e
		denom += e
	}
	for c := range probs {
		probs[c] = float32(exps[c] / denom)
	}

	best := 0
	bestP := probs[0]
	for c := 1; c < w.NumClasses; c++ {
		if probs[c] > bestP {
			bestP = probs[c]
			best = c
		}
	}
	return logits, probs, best, nil
}

func isFinite(v float32) bool {
	return !math.IsNaN(float64(v)) && !math.IsInf(float64(v), 0)
}
