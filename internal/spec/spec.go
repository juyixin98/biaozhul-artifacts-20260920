// Package spec defines the on-the-wire and on-disk data formats for SynapticGo:
// dataset files, model weights and their canonical hashes, and model metrics.
// Keeping the formats in one place lets the dataset upload path, model
// registry and inference engine agree on validation rules.
package spec

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
)

// Dataset is the canonical JSON content of an uploaded dataset file. Only
// structural validation belongs here; semantic checks (labels in range etc.)
// happen when a model is registered against the data.
//
// Features are flat float64 vectors; examples may optionally carry a class
// label (index into the model's class table).
type Dataset struct {
	Dim      int           `json:"dim"`
	Examples []DataExample `json:"examples"`
}

// DataExample is one labelled or unlabelled row.
type DataExample struct {
	X     []float64 `json:"x"`
	Label *int      `json:"label,omitempty"`
}

// MaxDatasetBytes bounds dataset files accepted by the validator. The
// service-level HTTP body limit (SYN_MAX_JSON_BYTES for metadata) is separate;
// datasets travel as chunked binary uploads.
const MaxDatasetBytes = 256 << 20

// ParseDataset decodes a dataset JSON document and validates its structure.
func ParseDataset(raw []byte) (*Dataset, error) {
	if len(raw) > MaxDatasetBytes {
		return nil, fmt.Errorf("dataset exceeds %d bytes", MaxDatasetBytes)
	}
	var d Dataset
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := dec.Decode(&d); err != nil {
		return nil, fmt.Errorf("invalid dataset JSON: %w", err)
	}
	if d.Dim <= 0 {
		return nil, fmt.Errorf("dataset dim must be positive")
	}
	if len(d.Examples) == 0 {
		return nil, fmt.Errorf("dataset has no examples")
	}
	for i, ex := range d.Examples {
		if len(ex.X) != d.Dim {
			return nil, fmt.Errorf("example %d: expected %d features, got %d", i, d.Dim, len(ex.X))
		}
		for j, v := range ex.X {
			if math.IsNaN(v) || math.IsInf(v, 0) {
				return nil, fmt.Errorf("example %d feature %d is NaN or Inf", i, j)
			}
		}
		if ex.Label != nil && (*ex.Label < 0) {
			return nil, fmt.Errorf("example %d has negative label", i)
		}
	}
	return &d, nil
}

// Weights is the only supported parameterisation: one linear layer
//
//	logits[k] = W[k] · x + b[k]
//
// followed by softmax. JSON layout (canonicalised for hashing):
//
//	{"type":"linear_softmax","version":1,"dim":D,"classes":[...],"W":[[...K rows, D cols...],"b":[...K...]}
type Weights struct {
	Type    string      `json:"type"`
	Version int         `json:"version"`
	Dim     int         `json:"dim"`
	Classes []string    `json:"classes"`
	W       [][]float64 `json:"W"`
	B       []float64   `json:"b"`
}

const weightsType = "linear_softmax"

// ValidateWeights checks shape consistency and rejects non-finite parameters.
func ValidateWeights(w *Weights) error {
	if w.Type != weightsType {
		return fmt.Errorf("unsupported weights type %q (only %q)", w.Type, weightsType)
	}
	if w.Version != 1 {
		return fmt.Errorf("unsupported weights version %d", w.Version)
	}
	k := len(w.Classes)
	if k < 2 {
		return fmt.Errorf("model needs at least 2 classes, got %d", k)
	}
	if w.Dim <= 0 {
		return fmt.Errorf("weight dim must be positive")
	}
	if len(w.B) != k {
		return fmt.Errorf("bias length %d != classes %d", len(w.B), k)
	}
	if len(w.W) != k {
		return fmt.Errorf("W has %d rows != classes %d", len(w.W), k)
	}
	seen := make(map[string]struct{}, k)
	for i, name := range w.Classes {
		if name == "" {
			return fmt.Errorf("class %d has empty name", i)
		}
		if _, dup := seen[name]; dup {
			return fmt.Errorf("duplicate class name %q", name)
		}
		seen[name] = struct{}{}
		if len(w.W[i]) != w.Dim {
			return fmt.Errorf("W row %d has %d columns != dim %d", i, len(w.W[i]), w.Dim)
		}
		for j, v := range w.W[i] {
			if !isFinite(v) {
				return fmt.Errorf("W[%d][%d] is NaN or Inf", i, j)
			}
		}
		if !isFinite(w.B[i]) {
			return fmt.Errorf("b[%d] is NaN or Inf", i)
		}
	}
	return nil
}

// CanonicalWeights serialises weights in a deterministic form and returns the
// bytes plus their SHA-256. The same logical weights always produce the same
// hash regardless of input field ordering or whitespace.
func CanonicalWeights(w *Weights) (raw []byte, sha256hex string, err error) {
	if err := ValidateWeights(w); err != nil {
		return nil, "", err
	}
	// Round-trip through a typed struct (which sorts nothing, but fixes key
	// order) using compact encoding. Go's encoding/json emits struct fields in
	// declaration order, so the output is canonical.
	buf, err := json.Marshal(w)
	if err != nil {
		return nil, "", fmt.Errorf("canonicalise weights: %w", err)
	}
	sum := sha256.Sum256(buf)
	return buf, hex.EncodeToString(sum[:]), nil
}

// ParseWeights decodes and validates weights JSON.
func ParseWeights(raw []byte) (*Weights, error) {
	var w Weights
	if err := json.Unmarshal(raw, &w); err != nil {
		return nil, fmt.Errorf("invalid weights JSON: %w", err)
	}
	if err := ValidateWeights(&w); err != nil {
		return nil, err
	}
	return &w, nil
}

// ClassesHash binds a model version to an exact ordered class table. Versions
// with different classes_hash are never compared.
func ClassesHash(classes []string) string {
	buf, _ := json.Marshal(classes)
	sum := sha256.Sum256(buf)
	return hex.EncodeToString(sum[:])
}

// Metrics is the free-form evaluation record stored with a model version.
// Numeric leaf values may be compared between versions; everything else is
// reported as-is.
type Metrics map[string]any

// NumericMetric extracts a float64 metric by key. ok is false if absent or
// non-numeric.
func (m Metrics) NumericMetric(key string) (float64, bool) {
	v, ok := m[key]
	if !ok {
		return 0, false
	}
	f, ok := v.(float64)
	return f, ok
}

func isFinite(v float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0)
}
