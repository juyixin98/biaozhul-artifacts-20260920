package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestCLIEndToEnd 在临时目录构建 raftrun 二进制并跑一个真实场景，
// 校验 stdout 是合法结果 JSON、安全断言通过、退出码为 0。
func TestCLIEndToEnd(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping CLI build in short mode")
	}
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(t.TempDir(), "raftrun")
	build := exec.Command("go", "build", "-o", bin, "./cmd/raftrun")
	build.Dir = root
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}

	// 复制一个场景到临时目录并改写 dataDir，避免污染源码树。
	scSrc := filepath.Join(root, "examples", "basic-replication.json")
	raw, err := os.ReadFile(scSrc)
	if err != nil {
		t.Fatal(err)
	}
	var sc map[string]any
	if err := json.Unmarshal(raw, &sc); err != nil {
		t.Fatal(err)
	}
	work := t.TempDir()
	sc["dataDir"] = filepath.Join(work, "state")
	sc["trace"] = false
	scPath := filepath.Join(work, "scenario.json")
	scBytes, _ := json.MarshalIndent(sc, "", "  ")
	if err := os.WriteFile(scPath, scBytes, 0o644); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command(bin, "-scenario", scPath)
	cmd.Dir = work
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("run exit error: %v\noutput:\n%s", err, out)
	}
	var res struct {
		InvariantCheck struct {
			Passed bool     `json:"passed"`
			Errors []string `json:"errors"`
		} `json:"invariantCheck"`
		Committed []map[string]any `json:"committed"`
	}
	if err := json.Unmarshal(out, &res); err != nil {
		t.Fatalf("invalid result JSON: %v\n%s", err, out)
	}
	if !res.InvariantCheck.Passed {
		t.Fatalf("invariants failed: %v", res.InvariantCheck.Errors)
	}
	if len(res.Committed) != 3 {
		t.Fatalf("want 3 committed entries, got %d", len(res.Committed))
	}

	// 持久化文件确实被写到磁盘（证明状态落盘）。
	matches, _ := filepath.Glob(filepath.Join(work, "state", "node*", "state.json"))
	if len(matches) != 3 {
		t.Fatalf("want 3 persisted state files, got %v", matches)
	}
}

// TestCLIBadScenario 校验场景文件非法时以非零码退出。
func TestCLIBadScenario(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping CLI build in short mode")
	}
	root, _ := filepath.Abs(filepath.Join("..", ".."))
	bin := filepath.Join(t.TempDir(), "raftrun")
	build := exec.Command("go", "build", "-o", bin, "./cmd/raftrun")
	build.Dir = root
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	work := t.TempDir()
	bad := filepath.Join(work, "bad.json")
	if err := os.WriteFile(bad, []byte(`{"nodeCount":3,"endTime":100}`), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(bin, "-scenario", bad)
	cmd.Dir = work
	if err := cmd.Run(); err == nil {
		t.Fatal("invalid scenario (missing heartbeat) should exit non-zero")
	}
}
