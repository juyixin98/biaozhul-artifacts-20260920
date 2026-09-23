package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// buildCLI compiles the cbcast binary once for the CLI tests.
func buildCLI(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "cbcast")
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/cbcast")
	// Tests run with cwd=cmd/cbcast; point at the module root.
	cmd.Dir = filepath.Join("..", "..")
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("build cbcast: %v", err)
	}
	return bin
}

func runCLI(t *testing.T, bin string, args ...string) (string, string, error) {
	cmd := exec.Command(bin, args...)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	return stdout.String(), stderr.String(), err
}

func TestCLIRunsFileRequest(t *testing.T) {
	bin := buildCLI(t)
	dir := t.TempDir()
	in := filepath.Join(dir, "req.json")
	out := filepath.Join(dir, "res.json")
	if err := os.WriteFile(in, []byte(`{
	  "nodes": ["A","B"],
	  "network": {"default": {"baseDelay": 1}},
	  "broadcasts": [{"time":0,"from":"A","body":"hi"}]
	}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, stderr, err := runCLI(t, bin, "-in", in, "-out", out); err != nil {
		t.Fatalf("cbcast failed: %v %s", err, stderr)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var res map[string]any
	if err := json.Unmarshal(raw, &res); err != nil {
		t.Fatalf("result is not valid JSON: %v", err)
	}
	if _, ok := res["diagnostics"]; !ok {
		t.Fatal("result missing diagnostics section")
	}
}

func TestCLIGeneratorRoundTrip(t *testing.T) {
	bin := buildCLI(t)
	stdout, stderr, err := runCLI(t, bin, "-gen", "chain", "-nodes", "3", "-loss", "-run")
	if err != nil {
		t.Fatalf("gen chain -run: %v %s", err, stderr)
	}
	var res map[string]any
	if err := json.Unmarshal([]byte(stdout), &res); err != nil {
		t.Fatalf("generator result is not JSON: %v", err)
	}
	diag := res["diagnostics"].(map[string]any)
	missing := diag["permanentMissing"].([]any)
	if len(missing) == 0 {
		t.Fatal("loss chain should report permanent missing messages")
	}
}

func TestCLIRejectsInvalidRequest(t *testing.T) {
	bin := buildCLI(t)
	dir := t.TempDir()
	in := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(in, []byte(`{"nodes":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	_, stderr, err := runCLI(t, bin, "-in", in)
	if err == nil {
		t.Fatal("invalid request should exit non-zero")
	}
	if !strings.Contains(stderr, "cbcast:") {
		t.Fatalf("error output should be prefixed with cbcast:, got %q", stderr)
	}
}
