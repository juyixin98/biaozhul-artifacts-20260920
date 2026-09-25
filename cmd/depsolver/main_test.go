package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestCLISubprocess 通过编译出的二进制端到端验证 resolve / plan 子命令。
func TestCLISubprocess(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping subprocess test in -short mode")
	}
	bin := filepath.Join(t.TempDir(), "depsolver")
	build := exec.Command("go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}

	t.Run("resolve backtrack", func(t *testing.T) {
		cmd := exec.Command(bin, "resolve", "--request",
			filepath.Join(root, "examples", "resolve-backtrack.json"))
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("run: %v\n%s", err, out)
		}
		var res map[string]any
		if err := json.Unmarshal(out, &res); err != nil {
			t.Fatal(err)
		}
		if res["satisfiable"] != true {
			t.Fatalf("expected SAT: %s", out)
		}
	})

	t.Run("resolve unsat exits 0 with conflict body", func(t *testing.T) {
		// 无解是正常业务结果，CLI 以退出码 0 返回结构化冲突（区别于用法/IO 错误）。
		cmd := exec.Command(bin, "resolve", "--request",
			filepath.Join(root, "examples", "resolve-unsat.json"))
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("expected exit 0 on UNSAT: %v\n%s", err, out)
		}
		var res map[string]any
		if err := json.Unmarshal(out, &res); err != nil {
			t.Fatal(err)
		}
		if res["satisfiable"] != false {
			t.Fatalf("expected UNSAT: %s", out)
		}
	})

	t.Run("plan writes separated files", func(t *testing.T) {
		workspace := t.TempDir()
		cache := t.TempDir()
		reqPath := filepath.Join(t.TempDir(), "plan.json")
		src, err := os.ReadFile(filepath.Join(root, "examples", "plan-request.json"))
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		if err := json.Unmarshal(src, &body); err != nil {
			t.Fatal(err)
		}
		body["workspaceDir"] = workspace
		body["cacheDir"] = cache
		raw, _ := json.Marshal(body)
		if err := os.WriteFile(reqPath, raw, 0o644); err != nil {
			t.Fatal(err)
		}
		cmd := exec.Command(bin, "plan", "--request", reqPath)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("plan: %v\n%s", err, out)
		}
		if !strings.Contains(string(out), "depsolver.lock/v1") {
			t.Fatalf("output missing lockfile schema: %s", out)
		}
		if _, err := os.Stat(filepath.Join(workspace, "depsolver.lock.json")); err != nil {
			t.Errorf("lockfile missing in workspace: %v", err)
		}
		matches, _ := filepath.Glob(filepath.Join(cache, "resolve-*.json"))
		if len(matches) != 1 {
			t.Errorf("expected one cache record, got %v", matches)
		}
	})

	t.Run("missing request flag errors", func(t *testing.T) {
		cmd := exec.Command(bin, "resolve")
		err := cmd.Run()
		if err == nil {
			t.Fatal("expected nonzero exit")
		}
	})
}
