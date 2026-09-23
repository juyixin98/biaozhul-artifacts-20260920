package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func runCLI(t *testing.T, args []string, input string) map[string]any {
	t.Helper()
	var out bytes.Buffer
	err := run(args, strings.NewReader(input), &out)
	if err != nil {
		t.Fatalf("run(%v) error: %v", args, err)
	}
	var m map[string]any
	if err := json.Unmarshal(out.Bytes(), &m); err != nil {
		t.Fatalf("output not JSON: %v\n%s", err, out.String())
	}
	return m
}

const safeCfg = `{
  "config": {
    "nodes": [
      {"id": "n1", "weight": 1, "domain": "a"},
      {"id": "n2", "weight": 1, "domain": "b"},
      {"id": "n3", "weight": 1, "domain": "c"}
    ],
    "read_quorum": 2, "write_quorum": 2, "tolerate_domains": 1
  }
}`

func TestCLIAnalyzeSafe(t *testing.T) {
	m := runCLI(t, []string{"analyze"}, safeCfg)
	r := m["report"].(map[string]any)
	if r["ww_safe"] != true || r["rw_safe"] != true || r["available"] != true {
		t.Fatalf("safe config misreported: %v", r)
	}
}

func TestCLIAllUnsafeWithWitness(t *testing.T) {
	input := `{
      "config": {
        "nodes": [
          {"id": "n1", "weight": 1, "domain": "a"},
          {"id": "n2", "weight": 1, "domain": "b"},
          {"id": "n3", "weight": 1, "domain": "c"}
        ],
        "read_quorum": 1, "write_quorum": 2, "tolerate_domains": 1
      }
    }`
	m := runCLI(t, []string{"all"}, input)
	r := m["report"].(map[string]any)
	if r["rw_safe"] != false {
		t.Fatal("expected rw_safe=false")
	}
	ce := r["minimal_rw_counterexample"].(map[string]any)
	witness := ce["witness"].(map[string]any)
	res := witness["result"].(map[string]any)
	if res["violation_runs"].(float64) != 1 {
		t.Fatalf("witness must reproduce one violation, got %v", res)
	}
}

func TestCLIInvalidJSONExit(t *testing.T) {
	var out bytes.Buffer
	err := run(nil, strings.NewReader("{not json"), &out)
	if err == nil {
		t.Fatal("expected clientError")
	}
}

func TestCLISimulateRequiresSim(t *testing.T) {
	var out bytes.Buffer
	err := run([]string{"simulate"}, strings.NewReader(safeCfg), &out)
	if err == nil || !strings.Contains(err.Error(), `"sim" section`) {
		t.Fatalf("expected sim-section error, got %v", err)
	}
}

func TestCLIUnknownAction(t *testing.T) {
	var out bytes.Buffer
	err := run([]string{"frobnicate"}, strings.NewReader(safeCfg), &out)
	if err == nil {
		t.Fatal("expected usage error")
	}
}

func TestCLISimulateScripted(t *testing.T) {
	input := `{
      "config": {
        "nodes": [
          {"id": "n1", "weight": 1, "domain": "a"},
          {"id": "n2", "weight": 1, "domain": "b"},
          {"id": "n3", "weight": 1, "domain": "c"}
        ],
        "read_quorum": 2, "write_quorum": 2
      },
      "sim": {
        "seed": 1, "runs": 1, "operations": 2, "kind": "ww",
        "horizon": 40, "client_timeout": 100, "max_attempts": 1,
        "network": {"loss_rate": 0.6, "duplicate_rate": 0.3, "min_delay": 1, "max_delay": 3}
      }
    }`
	m := runCLI(t, []string{"simulate"}, input)
	s := m["simulation"].(map[string]any)
	if s["runs"].(float64) != 1 {
		t.Fatal("scripted/stochastic run count wrong")
	}
}
