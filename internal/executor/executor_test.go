package executor

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestPolicyRejectsNonBashAndTraversal(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "s.sh"), []byte(":"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := NewPolicy("/bin/sh", dir); err == nil {
		t.Fatal("non-bash interpreter must be rejected")
	}
	p, err := NewPolicy(DefaultInterpreter, dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, argv := range [][]string{
		{"/bin/cat", "/etc/passwd"},
		{"sh", "s.sh"},
		{"bash", "../escape.sh"},
		{"bash", "/etc/passwd"},
	} {
		if _, err := p.Resolve(argv); err == nil {
			t.Fatalf("Resolve(%v) must fail", argv)
		}
	}
}

func TestRunHappyPathAndCleanup(t *testing.T) {
	fix := t.TempDir()
	script := filepath.Join(fix, "build.sh")
	if err := os.WriteFile(script, []byte(`set -euo pipefail
cat "$IN_DATA" > "$OUT_ONE"
echo two > "$OUT_TWO"
`), 0o755); err != nil {
		t.Fatal(err)
	}
	p, err := NewPolicy(DefaultInterpreter, fix)
	if err != nil {
		t.Fatal(err)
	}
	rc, err := p.Resolve([]string{"bash", "build.sh"})
	if err != nil {
		t.Fatal(err)
	}
	workRoot := t.TempDir()
	inDir := t.TempDir()
	inFile := filepath.Join(inDir, "data")
	if err := os.WriteFile(inFile, []byte("payload\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := p.Run(context.Background(), rc, RunOptions{
		WorkRoot: workRoot,
		Inputs:   []MaterializedInput{{Slot: "DATA", Path: inFile}},
		Outputs:  []string{"ONE", "TWO"},
	})
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(res.Outputs[0].Path)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "payload\n" {
		t.Fatalf("unexpected output %q", b)
	}
	if err := os.RemoveAll(res.WorkDir); err != nil {
		t.Fatal(err)
	}
	if entries, err := os.ReadDir(workRoot); err != nil || len(entries) != 0 {
		t.Fatalf("work root should be empty after cleanup, got %v %v", entries, err)
	}
}

func TestRunFailsOnMissingDeclaredOutput(t *testing.T) {
	fix := t.TempDir()
	script := filepath.Join(fix, "build.sh")
	if err := os.WriteFile(script, []byte(`echo hi > "$OUT_ONE"`), 0o755); err != nil {
		t.Fatal(err)
	}
	p, err := NewPolicy(DefaultInterpreter, fix)
	if err != nil {
		t.Fatal(err)
	}
	rc, err := p.Resolve([]string{"bash", "build.sh"})
	if err != nil {
		t.Fatal(err)
	}
	res, err := p.Run(context.Background(), rc, RunOptions{
		WorkRoot: t.TempDir(),
		Outputs:  []string{"ONE", "TWO"},
	})
	if err == nil {
		t.Fatal("missing declared output must error")
	}
	if res == nil || len(res.Outputs) != 1 {
		t.Fatalf("result should still list the one produced output, got %+v", res)
	}
}
