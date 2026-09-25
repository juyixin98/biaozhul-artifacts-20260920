package batchagg

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestSimulator_PartialFailureAndIndependentOutputs(t *testing.T) {
	sim := &Simulator{DefaultModel: "sim-llm-1"}

	items := []Item{
		{ID: "r0", Key: "m", Payload: mustJSON(t, InferRequest{Prompt: "hello", MaxTokens: 3})},
		{ID: "r1", Key: "m", Payload: []byte("{not-json")},
		{ID: "r2", Key: "m", Payload: mustJSON(t, InferRequest{Prompt: "boom", Fail: true, FailureCode: "E42"})},
		{ID: "r3", Key: "m", Payload: mustJSON(t, InferRequest{Model: "custom", Prompt: "hi"})},
	}

	results, err := sim.Execute(context.Background(), "batch-X", items)
	if err != nil {
		t.Fatalf("Execute must not return a whole-batch error: %v", err)
	}
	if len(results) != 4 {
		t.Fatalf("results len=%d want 4", len(results))
	}

	// r0 success, output decodes to a response carrying batch id and index.
	if results[0].Err != nil {
		t.Fatalf("r0: %v", results[0].Err)
	}
	var ok0 InferResponse
	if err := json.Unmarshal(results[0].Output, &ok0); err != nil {
		t.Fatal(err)
	}
	if ok0.Model != "sim-llm-1" || ok0.BatchID != "batch-X" || ok0.Index != 0 ||
		ok0.PromptChars != 5 || ok0.TokensOut != 3 || ok0.Echo != "hello" {
		t.Fatalf("r0 response mismatch: %+v", ok0)
	}

	// r1 malformed: individual failure only.
	if results[1].Err == nil || !strings.Contains(results[1].Err.Error(), "invalid inference request") {
		t.Fatalf("r1 err=%v", results[1].Err)
	}

	// r2 flagged failure: typed error, individual.
	if !errors.Is(results[2].Err, ErrSimulatedInference) ||
		!strings.Contains(results[2].Err.Error(), "E42") {
		t.Fatalf("r2 err=%v", results[2].Err)
	}

	// r3 explicit model passes unaffected.
	if results[3].Err != nil {
		t.Fatalf("r3: %v", results[3].Err)
	}
	var ok3 InferResponse
	if err := json.Unmarshal(results[3].Output, &ok3); err != nil {
		t.Fatal(err)
	}
	if ok3.Model != "custom" || ok3.Index != 3 {
		t.Fatalf("r3 mismatch: %+v", ok3)
	}
}

func TestSimulator_EmptyPromptRejected(t *testing.T) {
	sim := &Simulator{}
	results, err := sim.Execute(context.Background(), "b", []Item{
		{ID: "e", Key: "m", Payload: mustJSON(t, InferRequest{Prompt: ""})},
	})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Err == nil || !strings.Contains(results[0].Err.Error(), "prompt is empty") {
		t.Fatalf("err=%v", results[0].Err)
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
