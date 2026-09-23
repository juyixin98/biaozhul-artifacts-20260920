package jsonio

import (
	"strings"
	"testing"
)

const validReq = `{
  "nodes": ["A", "B", "C"],
  "bufferCap": 4,
  "network": {
    "seed": 42,
    "default": {"baseDelay": 1, "jitter": 0.5, "loss": 0.1, "duplicate": 0.05},
    "dropIds": [{"msgId": "A1", "dst": ["C"]}],
    "holdIds": [{"msgId": "B1", "dst": ["C"], "delay": 10}]
  },
  "broadcasts": [
    {"time": 0, "from": "A", "body": "hello"},
    {"time": 2, "from": "B", "body": "world"}
  ]
}`

func TestRunValidRequest(t *testing.T) {
	res, err := Run([]byte(validReq))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res == nil || len(res.Nodes) != 3 {
		t.Fatalf("bad result: %+v", res)
	}
	// A1 was force-dropped at C; B1 (clock A=1,B=1) must be buffered there.
	found := false
	for _, nf := range res.Nodes {
		if nf.Name != "C" {
			continue
		}
		for _, b := range nf.Buffered {
			if b.MsgID == "B1" {
				found = true
			}
		}
	}
	if !found {
		t.Fatalf("B1 should be buffered at C: %+v", res.Nodes)
	}
}

func TestHoldAcceptsArrayForm(t *testing.T) {
	raw := strings.Replace(validReq,
		`"delay": 10`, `"delay": [10, 12]`, 1)
	res, err := Run([]byte(raw))
	if err != nil {
		t.Fatalf("array delay rejected: %v", err)
	}
	// Two late duplicate copies of B1 to C.
	if res.Stats.Duplicates < 1 {
		t.Fatalf("expected duplicates from held copies, got %d", res.Stats.Duplicates)
	}
}

func TestValidationRejectsBadInput(t *testing.T) {
	cases := map[string]string{
		"no nodes":            `{"nodes":[]}`,
		"unknown broadcaster": `{"nodes":["A"],"broadcasts":[{"time":0,"from":"Z"}]}`,
		"bad probability":     `{"nodes":["A","B"],"network":{"default":{"loss":2}}}`,
		"unknown drop dst":    `{"nodes":["A","B"],"network":{"dropIds":[{"msgId":"A1","dst":["Z"]}]}}`,
		"unknown field":       `{"nodes":["A"],"bogus":1}`,
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Run([]byte(raw)); err == nil {
				t.Fatalf("%s: expected validation error", name)
			}
		})
	}
}

func TestMaxTimeReportsInFlightNotLost(t *testing.T) {
	raw := `{
	  "nodes": ["A","B"],
	  "maxTime": 2,
	  "network": {"default": {"baseDelay": 10}},
	  "broadcasts": [{"time":0,"from":"A"}]
	}`
	res, err := Run([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if res.Complete {
		t.Fatal("run should be cut off")
	}
	if len(res.Diagnostics.PermanentMissing) != 0 {
		t.Fatalf("in-flight must not be permanent loss: %+v", res.Diagnostics.PermanentMissing)
	}
	if len(res.Diagnostics.Blocked) != 1 || res.Diagnostics.Blocked[0].State != "in-flight" {
		t.Fatalf("want one in-flight blocked entry, got %+v", res.Diagnostics.Blocked)
	}
}

func TestDefaultBufferCap(t *testing.T) {
	req := Request{Nodes: []string{"A", "B"}}
	if err := req.Validate(); err != nil {
		t.Fatal(err)
	}
	if req.BufferCap != DefaultBufferCap {
		t.Fatalf("default buffer cap = %d", req.BufferCap)
	}
}
