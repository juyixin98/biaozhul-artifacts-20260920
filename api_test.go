package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

func TestAPI_ReplicaLifecycle(t *testing.T) {
	h := newHTTPTest(t)

	// Empty store lists as [] rather than null.
	var list struct {
		Replicas []string `json:"replicas"`
	}
	h.mustStatus(http.MethodGet, "/replicas", nil, &list, http.StatusOK)
	if len(list.Replicas) != 0 {
		t.Fatalf("empty store replicas = %v", list.Replicas)
	}

	// Health.
	h.mustStatus(http.MethodGet, "/health", nil, nil, http.StatusOK)

	h.createReplica("A")
	h.mustStatus(http.MethodGet, "/replicas", nil, &list, http.StatusOK)
	if len(list.Replicas) != 1 || list.Replicas[0] != "A" {
		t.Fatalf("replicas after create = %v", list.Replicas)
	}

	// Duplicate id -> 409.
	h.mustStatus(http.MethodPost, "/replicas", map[string]string{"id": "A"}, nil, http.StatusConflict)
	// Empty id -> 400.
	h.mustStatus(http.MethodPost, "/replicas", map[string]string{"id": ""}, nil, http.StatusBadRequest)

	// Snapshot of an empty replica.
	var snap Snapshot
	h.mustStatus(http.MethodGet, "/replicas/A", nil, &snap, http.StatusOK)
	if snap.Replica != "A" {
		t.Fatalf("snapshot replica = %q", snap.Replica)
	}

	// Delete happy path, then delete again -> 404.
	h.mustStatus(http.MethodDelete, "/replicas/A", nil, nil, http.StatusOK)
	h.mustStatus(http.MethodDelete, "/replicas/A", nil, nil, http.StatusNotFound)
}

func TestAPI_Reset(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.put("A", "k", "v")
	h.mustStatus(http.MethodPost, "/replicas/reset", nil, nil, http.StatusOK)
	if status := h.do(http.MethodGet, "/replicas/A", nil, nil); status != http.StatusNotFound {
		t.Fatalf("after reset GET replica -> %d, want 404", status)
	}
}

func TestAPI_StrictJSONDecoding(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")

	// Unknown field rejected.
	status := h.do(http.MethodPut, "/replicas/A/keys/k",
		map[string]string{"value": "x", "unexpected": "y"}, nil)
	if status != http.StatusBadRequest {
		t.Fatalf("unknown field -> %d, want 400", status)
	}

	// Two JSON values in one body rejected.
	req, _ := http.NewRequest(http.MethodPut, h.host+"/replicas/A/keys/k",
		bytes.NewReader([]byte(`{"value":"x"}garbage`)))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("trailing data -> %d, want 400", resp.StatusCode)
	}
}

func TestAPI_ReadWriteErrorCodes(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")

	// Unknown key -> 404.
	h.mustStatus(http.MethodGet, "/replicas/A/keys/missing", nil, nil, http.StatusNotFound)
	// Write to unknown replica -> 404.
	h.mustStatus(http.MethodPut, "/replicas/Z/keys/k", map[string]string{"value": "x"}, nil, http.StatusNotFound)

	h.put("A", "k", "v")
	// Merge referencing an unknown context version -> 400 (context not found).
	h.mustStatus(http.MethodPost, "/replicas/A/keys/k/merge",
		map[string]any{"value": "m", "context": []string{"A-99"}}, nil, http.StatusBadRequest)
	// Duplicate id inside context -> 400.
	h.mustStatus(http.MethodPost, "/replicas/A/keys/k/merge",
		map[string]any{"value": "m", "context": []string{"A-1", "A-1"}}, nil, http.StatusBadRequest)
}

func TestAPI_DeliverValidation(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")

	// Empty versions array -> 400.
	h.mustStatus(http.MethodPost, "/replicas/A/keys/k/messages",
		map[string]any{"versions": []any{}}, nil, http.StatusBadRequest)
	// Malformed version (empty clock) -> 400.
	bad := Version{ID: "X-1", Origin: "X", Clock: Clock{}}
	raw, _ := json.Marshal(map[string]any{"versions": []Version{bad}})
	req, _ := http.NewRequest(http.MethodPost, h.host+"/replicas/A/keys/k/messages", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed version -> %d, want 400", resp.StatusCode)
	}
	// Deliver to unknown replica -> 404.
	h.mustStatus(http.MethodPost, "/replicas/Z/keys/k/messages",
		map[string]any{"versions": []Version{{ID: "X-1", Origin: "X", Clock: Clock{"X": 1}}}},
		nil, http.StatusNotFound)
}

func TestAPI_SyncValidation(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")

	// Missing fields.
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A"}, nil, http.StatusBadRequest)
	// Bad mode.
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A", "to": "B", "mode": "sideways"}, nil, http.StatusBadRequest)
	// Same replica -> 400.
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A", "to": "A"}, nil, http.StatusBadRequest)
	// Replica in the URL is neither endpoint -> 400.
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "B", "to": "C"}, nil, http.StatusBadRequest)
	// Unknown replica referenced -> 404.
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A", "to": "C"}, nil, http.StatusNotFound)

	// Default mode is one-way and works.
	h.put("A", "k", "a")
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A", "to": "B"}, nil, http.StatusOK)
}

func TestClockOrderString(t *testing.T) {
	cases := map[ClockOrder]string{
		ClockEqual:      "equal",
		ClockBefore:     "before",
		ClockAfter:      "after",
		ClockConcurrent: "concurrent",
	}
	for ord, want := range cases {
		if got := ord.String(); got != want {
			t.Errorf("%v.String() = %q, want %q", ord, got, want)
		}
	}
}
