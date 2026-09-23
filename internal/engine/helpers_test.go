package engine

import (
	"encoding/json"
	"os"
	"testing"

	"causal-broadcast/internal/simnet"
)

func PolicyNoFaults() simnet.Policy {
	return simnet.Policy{MinDelayMs: 1, MaxDelayMs: 4}
}

func PolicyRandom(loss, dup float64, minD, maxD int64) simnet.Policy {
	return simnet.Policy{
		LossRate: loss, DuplicateRate: dup, MinDelayMs: minD, MaxDelayMs: maxD,
	}
}

// faultShim is a compact test builder for simnet.Fault.
type faultShim struct {
	origin  string
	seq     int
	from    string
	to      string
	action  string
	delayMs int64
}

func (s faultShim) toFault() simnet.Fault {
	return simnet.Fault{
		OriginNode: s.origin, OriginSeq: s.seq, From: s.from, To: s.to,
		Action: s.action, DelayMs: s.delayMs,
	}
}

type faultList []faultShim

func (s faultList) toFaults() []simnet.Fault {
	out := make([]simnet.Fault, len(s))
	for i, f := range s {
		out[i] = f.toFault()
	}
	return out
}

// shim with variadic-style methods is overkill; keep the slice helper used in tests.
var _ = faultList(nil)

func mustReadRequest(t *testing.T, path string) *Request {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var req Request
	if err := json.Unmarshal(b, &req); err != nil {
		t.Fatalf("unmarshal %s: %v", path, err)
	}
	return &req
}
