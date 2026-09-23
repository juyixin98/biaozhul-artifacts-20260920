package runner

import (
	"strings"
	"testing"

	"cdbg/internal/graph"
)

func TestRunNonZeroExitAndCapture(t *testing.T) {
	r := New()
	tool := &graph.Tool{Name: "echoerr", ToolVersion: "1", Command: []string{"sh", "-c"}, Shell: false}
	// 非 shell 形式直接调用 sh -c 更精确地测退出码。
	node := &graph.Node{Name: "n", Tool: "echoerr", Args: []string{"echo out; echo err 1>&2; exit 3"}}
	res, err := r.Run(t.TempDir(), tool, node, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 3 {
		t.Fatalf("exit code = %d, want 3", res.ExitCode)
	}
	if strings.TrimSpace(res.Stdout) != "out" {
		t.Fatalf("stdout = %q", res.Stdout)
	}
	if strings.TrimSpace(res.Stderr) != "err" {
		t.Fatalf("stderr = %q", res.Stderr)
	}
}

func TestShellModePositionalArgs(t *testing.T) {
	r := New()
	tool := &graph.Tool{
		Name: "emit", ToolVersion: "1",
		Command: []string{"printf '%s|%s' \"$1\" \"$2\""},
		Shell:   true,
	}
	node := &graph.Node{
		Name: "n", Tool: "emit",
		Params: map[string]string{"who": "world"},
		Args:   []string{"hello", "{{.who}}"},
	}
	res, err := r.Run(t.TempDir(), tool, node, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || res.Stdout != "hello|world" {
		t.Fatalf("code=%d stdout=%q", res.ExitCode, res.Stdout)
	}
}

func TestEnvWhitelist(t *testing.T) {
	r := New()
	tool := &graph.Tool{
		Name: "envcheck", ToolVersion: "1",
		Command:     []string{"sh", "-c"},
		Shell:       false,
		DeclaredEnv: []string{"ALLOWED"},
		Env:         map[string]string{"FIXED": "yes"},
	}
	node := &graph.Node{
		Name: "n", Tool: "envcheck",
		Args: []string{"test \"$ALLOWED\" = keep && test \"$FIXED\" = yes && test -z \"$DENIED\""},
	}
	inherited := map[string]string{"ALLOWED": "keep", "DENIED": "leak"}
	res, err := r.Run(t.TempDir(), tool, node, inherited)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 {
		t.Fatalf("env isolation failed, code=%d stderr=%q", res.ExitCode, res.Stderr)
	}
}

func TestTimeout(t *testing.T) {
	r := New()
	tool := &graph.Tool{Name: "sleep", ToolVersion: "1", Command: []string{"sleep 2"}, Shell: true}
	node := &graph.Node{Name: "n", Tool: "sleep", TimeoutSec: 1}
	res, err := r.Run(t.TempDir(), tool, node, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut {
		t.Fatal("expected timeout")
	}
}
