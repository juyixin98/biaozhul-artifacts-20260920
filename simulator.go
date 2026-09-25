package batchagg

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// ErrSimulatedInference marks items the Simulator was asked to fail. Callers
// can use errors.Is to distinguish injected failures from real ones.
var ErrSimulatedInference = errors.New("simulated inference failure")

// InferRequest is the simulated inference payload for one item. Only Prompt is
// required. Fail is a test/ops hook: when true the item is deterministically
// failed by the simulator while its siblings still succeed.
type InferRequest struct {
	Model       string            `json:"model,omitempty"`
	Prompt      string            `json:"prompt"`
	MaxTokens   int               `json:"max_tokens,omitempty"`
	Temperature float64           `json:"temperature,omitempty"`
	Metadata    map[string]string `json:"metadata,omitempty"`
	// Fail, when true, makes this item fail independently within its batch.
	Fail bool `json:"fail,omitempty"`
	// FailureCode is surfaced verbatim in the error message when Fail is true.
	FailureCode string `json:"failure_code,omitempty"`
}

// InferResponse is the simulated per-item output.
type InferResponse struct {
	Model       string `json:"model"`
	PromptChars int    `json:"prompt_chars"`
	Echo        string `json:"echo"`
	TokensOut   int    `json:"tokens_out"`
	BatchID     string `json:"batch_id"`
	Index       int    `json:"index"`
}

// Simulator is an Executor that emulates an inference backend: each item is
// decoded independently, Fail-flagged items fail independently (partial batch
// failure), and the rest succeed. It never returns a whole-batch error itself;
// wrap it to simulate backend outages.
type Simulator struct {
	// DefaultModel is used when an item's request omits Model.
	DefaultModel string
}

// Execute implements Executor.
func (sim *Simulator) Execute(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error) {
	if sim.DefaultModel == "" {
		sim.DefaultModel = "sim-llm-1"
	}
	// Per-item decode: one malformed item fails just that item.
	reqs := make([]InferRequest, len(items))
	decodeErrs := make([]error, len(items))
	for i, it := range items {
		if err := json.Unmarshal(it.Payload, &reqs[i]); err != nil {
			decodeErrs[i] = fmt.Errorf("invalid inference request for item %q: %w", it.ID, err)
		} else if reqs[i].Prompt == "" {
			decodeErrs[i] = fmt.Errorf("invalid inference request for item %q: prompt is empty", it.ID)
		}
	}

	results := make([]*ItemResult, len(items))
	for i := range items {
		switch {
		case decodeErrs[i] != nil:
			results[i] = &ItemResult{Index: i, BatchID: batchID, Err: decodeErrs[i]}
			continue
		case reqs[i].Fail:
			code := reqs[i].FailureCode
			if code == "" {
				code = "INFERENCE_ERROR"
			}
			results[i] = &ItemResult{
				Index:   i,
				BatchID: batchID,
				Err:     fmt.Errorf("%w: item %q (%s)", ErrSimulatedInference, items[i].ID, code),
			}
			continue
		}

		req := reqs[i]
		model := req.Model
		if model == "" {
			model = sim.DefaultModel
		}
		tokensOut := req.MaxTokens
		if tokensOut <= 0 {
			tokensOut = 8
		}
		resp := InferResponse{
			Model:       model,
			PromptChars: len(req.Prompt),
			Echo:        req.Prompt,
			TokensOut:   tokensOut,
			BatchID:     batchID,
			Index:       i,
		}
		raw, err := json.Marshal(resp)
		if err != nil {
			results[i] = &ItemResult{Index: i, BatchID: batchID, Err: err}
			continue
		}
		results[i] = &ItemResult{Index: i, BatchID: batchID, Output: raw}
	}
	return results, nil
}
