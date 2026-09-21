package modelreg

import (
	"context"
	"encoding/json"

	"github.com/synapticgo/synapticgo/internal/httpx"
)

// MetricComparison holds one metric across the two versions.
type MetricComparison struct {
	Metric string   `json:"metric"`
	A      *float64 `json:"a"`
	B      *float64 `json:"b"`
	Delta  *float64 `json:"delta_b_minus_a"`
}

// CompareResponse is the comparison payload.
type CompareResponse struct {
	A              View               `json:"a"`
	B              View               `json:"b"`
	Comparable     bool               `json:"comparable"`
	Reason         string             `json:"reason,omitempty"`
	InputDim       *int               `json:"input_dim,omitempty"`
	EvalDatasetSHA *string            `json:"eval_dataset_sha,omitempty"`
	Metrics        []MetricComparison `json:"metrics,omitempty"`
}

// Compare validates that the two versions are measuring the same thing
// before comparing numeric metrics:
//
//   - ordered class tables must be identical (classes_hash),
//   - input dimensions must match,
//   - when either version recorded an evaluation dataset, both must record
//     the same content hash — otherwise the numbers describe different data.
//
// Mismatches return 422 rather than a silent apples-to-oranges report.
func (s *Service) Compare(ctx context.Context, ownerID, aID, bID int64) (*CompareResponse, error) {
	a, err := s.getOwned(ctx, ownerID, aID)
	if err != nil {
		return nil, err
	}
	b, err := s.getOwned(ctx, ownerID, bID)
	if err != nil {
		return nil, err
	}
	out := &CompareResponse{A: *a, B: *b}

	if a.ClassesHash != b.ClassesHash {
		out.Reason = "class tables differ; metrics over different label sets are not comparable"
		return out, httpx.ErrUnprocess(out.Reason)
	}
	if a.InputDim != b.InputDim {
		out.Reason = "input dimensions differ"
		return out, httpx.ErrUnprocess(out.Reason)
	}
	if (a.EvalDatasetSHA == nil) != (b.EvalDatasetSHA == nil) ||
		(a.EvalDatasetSHA != nil && *a.EvalDatasetSHA != *b.EvalDatasetSHA) {
		out.Reason = "evaluation datasets differ or are missing on one side; metrics are not comparable"
		return out, httpx.ErrUnprocess(out.Reason)
	}

	out.Comparable = true
	out.InputDim = &a.InputDim
	out.EvalDatasetSHA = a.EvalDatasetSHA

	var ma, mb specMetrics
	if err := json.Unmarshal(a.Metrics, &ma); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(b.Metrics, &mb); err != nil {
		return nil, err
	}
	// Union of keys, sorted.
	keys := map[string]struct{}{}
	for k := range ma {
		keys[k] = struct{}{}
	}
	for k := range mb {
		keys[k] = struct{}{}
	}
	sorted := make([]string, 0, len(keys))
	for k := range keys {
		sorted = append(sorted, k)
	}
	sortStrings(sorted)
	for _, k := range sorted {
		cmp := MetricComparison{Metric: k}
		av, aok := ma.numeric(k)
		bv, bok := mb.numeric(k)
		if aok {
			cmp.A = &av
		}
		if bok {
			cmp.B = &bv
		}
		if aok && bok {
			d := bv - av
			cmp.Delta = &d
		}
		out.Metrics = append(out.Metrics, cmp)
	}
	return out, nil
}

// specMetrics decodes metrics as raw values; only numeric leaves compare.
type specMetrics map[string]any

func (m specMetrics) numeric(k string) (float64, bool) {
	v, ok := m[k]
	if !ok {
		return 0, false
	}
	f, ok := v.(float64)
	return f, ok
}

func sortStrings(s []string) {
	// tiny dependency-free insertion sort (metric sets are small)
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}
