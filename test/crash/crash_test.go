// Package crash_test drives the real delta-update CLI as subprocesses and
// kills (os.Exit) it at every application stage. These are the acceptance
// fixtures for interrupt/crash recovery: a hard crash at any stage must leave
// the old artifact usable, and a plain re-run must converge to the new digest.
package crash_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"deltaupdate/internal/delta"
)

var (
	binaryPath string
	buildDir   string
)

// TestMain builds the real CLI once into a persistent temp dir; package-level
// t.TempDir() values are removed at the end of the test that created them,
// which would delete the binary mid-suite.
func TestMain(m *testing.M) {
	var err error
	buildDir, err = os.MkdirTemp("", "delta-crash-")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	binaryPath = filepath.Join(buildDir, "delta-update")
	cmd := exec.Command("go", "build", "-o", binaryPath, "deltaupdate/cmd/delta-update")
	cmd.Env = append(os.Environ(), "GOFLAGS=-mod=mod", "GOPROXY=off", "CGO_ENABLED=0")
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "build CLI: %v\n%s\n", err, out.String())
		os.RemoveAll(buildDir)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(buildDir)
	os.Exit(code)
}

// TestBuildBinary documents that the fixture binary was built in TestMain.
func TestBuildBinary(t *testing.T) {
	if _, err := os.Stat(binaryPath); err != nil {
		t.Fatalf("fixture binary missing: %v", err)
	}
}

func digestFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func mustExist(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected %s to exist: %v", path, err)
	}
}

func mustNotExist(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Stat(path); err == nil {
		t.Fatalf("expected %s to be absent", path)
	}
}

type fixture struct {
	dir    string
	cache  string
	old    []byte
	new    []byte
	oldSum string
	newSum string
	target string
	patch  string
}

func setupFixture(t *testing.T) fixture {
	t.Helper()
	if binaryPath == "" {
		TestBuildBinary(t)
	}
	dir := t.TempDir()
	cache := filepath.Join(dir, "cache")
	target := filepath.Join(dir, "work", "app", "artifact.bin")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	oldB := bytes.Repeat([]byte("OLD-BASE-ARTIFACT-"), 4096) // 73728 bytes
	newB := append([]byte{}, oldB...)
	// Insert near the start (shifts all blocks) + append + change a region.
	head := []byte(">>>CRASH-FIXTURE-INSERTION<<<")
	newB = append(newB[:123], append(head, newB[123:]...)...)
	newB = append(newB, []byte("\nTAIL LINE FOR CRASH TEST\n")...)
	if err := os.WriteFile(target, oldB, 0o644); err != nil {
		t.Fatal(err)
	}

	// Build patch through the library (deterministic), write JSON for CLI.
	p, err := delta.Generate(bytes.NewReader(oldB), int64(len(oldB)),
		bytes.NewReader(newB), int64(len(newB)), 1024)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := p.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	patchPath := filepath.Join(dir, "patch.json")
	if err := os.WriteFile(patchPath, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return fixture{
		dir: dir, cache: cache, old: oldB, new: newB,
		oldSum: delta.DigestBytes(oldB), newSum: delta.DigestBytes(newB),
		target: target, patch: patchPath,
	}
}

type runResult struct {
	exitCode int
	stdout   string
	stderr   string
}

// runApply executes the CLI apply with fault env vars.
func runApply(fx fixture, env map[string]string) runResult {
	cmd := exec.Command(binaryPath, "apply", "--target", fx.target, "--patch", fx.patch)
	base := []string{"GOPROXY=off"}
	cmdEnv := append(os.Environ(), base...)
	for k, v := range env {
		cmdEnv = append(cmdEnv, k+"="+v)
	}
	cmd.Env = cmdEnv
	var so, se bytes.Buffer
	cmd.Stdout = &so
	cmd.Stderr = &se
	err := cmd.Run()
	code := 0
	if ee, ok := err.(*exec.ExitError); ok {
		code = ee.ExitCode()
	} else if err != nil {
		code = -1
	}
	return runResult{exitCode: code, stdout: so.String(), stderr: se.String()}
}

// crashStages are exercised as hard os.Exit(42) crashes.
var crashStages = []string{
	"check-old",
	"space",
	"prepare",
	"write-delta",
	"sync",
	"verify-delta",
	"rename",
	"post-rename",
	"fsync-dir",
	"verify-new",
}

// TestCrashAtEveryStage: after each hard crash the old artifact must remain
// usable (either untouched, pre-rename, or already new, post-rename), and a
// fault-free re-run must converge to NewSum with exit code 0.
func TestCrashAtEveryStage(t *testing.T) {
	for _, stage := range crashStages {
		stage := stage
		t.Run(stage, func(t *testing.T) {
			fx := setupFixture(t)
			mustExist(t, fx.target)

			env := map[string]string{"DELTA_CRASH_STAGE": stage}
			if stage == "write-delta" {
				env["DELTA_FAULT_AFTER_BYTES"] = "4096"
			}
			rr := runApply(fx, env)
			if rr.exitCode != 42 {
				t.Fatalf("crash stage %s: want exit 42, got %d\nstdout:%s\nstderr:%s",
					stage, rr.exitCode, rr.stdout, rr.stderr)
			}

			// The artifact path must still exist and be either the old digest
			// (crash before rename) or the new digest (crash after rename).
			// In NO case may it be truncated/partial garbage.
			got := digestFile(t, fx.target)
			postRename := stage == "post-rename" || stage == "fsync-dir" || stage == "verify-new"
			switch {
			case postRename:
				if got != fx.newSum {
					t.Fatalf("stage %s: post-rename crash left non-new content %s", stage, got)
				}
			default:
				if got != fx.oldSum {
					t.Fatalf("stage %s: pre-rename crash did not preserve old artifact: %s", stage, got)
				}
			}

			// Re-run with no fault: must converge to new and exit 0.
			rr2 := runApply(fx, nil)
			if rr2.exitCode != 0 {
				t.Fatalf("stage %s: recovery re-run exit %d\nstdout:%s\nstderr:%s",
					stage, rr2.exitCode, rr2.stdout, rr2.stderr)
			}
			if digestFile(t, fx.target) != fx.newSum {
				t.Fatalf("stage %s: final digest != NewSum", stage)
			}
			// Result JSON must report correct digests.
			var out map[string]any
			if err := json.Unmarshal([]byte(rr2.stdout), &out); err != nil {
				t.Fatalf("parse apply output %q: %v", rr2.stdout, err)
			}
			if out["newDigest"] != fx.newSum {
				t.Fatalf("reported newDigest %v want %s", out["newDigest"], fx.newSum)
			}
			mustNotExist(t, fx.target+".delta.tmp")
		})
	}
}

// TestCrashThenCrashAgain: crash mid-write twice in a row (stale temp on the
// second attempt), then recover. Exercises stale-temp cleanup under the lock.
func TestCrashThenCrashAgain(t *testing.T) {
	fx := setupFixture(t)
	for i := 0; i < 2; i++ {
		rr := runApply(fx, map[string]string{
			"DELTA_CRASH_STAGE":       "write-delta",
			"DELTA_FAULT_AFTER_BYTES": fmt.Sprint(1000 + i*3000),
		})
		if rr.exitCode != 42 {
			t.Fatalf("crash %d: want 42 got %d (%s)", i, rr.exitCode, rr.stderr)
		}
		if digestFile(t, fx.target) != fx.oldSum {
			t.Fatalf("crash %d: old artifact not preserved", i)
		}
	}
	rr := runApply(fx, nil)
	if rr.exitCode != 0 {
		t.Fatalf("recovery failed: %d %s", rr.exitCode, rr.stderr)
	}
	if digestFile(t, fx.target) != fx.newSum {
		t.Fatal("final digest mismatch after repeated crashes")
	}
}

// TestCLIWrongBaseExitsDistinctCode: applying to an unexpected base must fail
// with a dedicated exit code and leave the file intact.
func TestCLIWrongBase(t *testing.T) {
	fx := setupFixture(t)
	// Overwrite target with unrelated bytes after patch was built.
	other := bytes.Repeat([]byte("completely-different-base!"), 4096)
	if err := os.WriteFile(fx.target, other, 0o644); err != nil {
		t.Fatal(err)
	}
	rr := runApply(fx, nil)
	if rr.exitCode != 10 {
		t.Fatalf("want exit 10 (wrong base), got %d: %s", rr.exitCode, rr.stderr)
	}
	if digestFile(t, fx.target) != delta.DigestBytes(other) {
		t.Fatal("wrong-base attempt mutated target")
	}
}

// TestCLICorruptPatchExitsDistinctCode: flip a byte in the patch file.
func TestCLICorruptPatch(t *testing.T) {
	fx := setupFixture(t)
	raw, err := os.ReadFile(fx.patch)
	if err != nil {
		t.Fatal(err)
	}
	// Find the patchSum JSON field and corrupt it.
	s := string(raw)
	idx := bytes.Index(raw, []byte(`"patchSum":"`))
	if idx < 0 {
		t.Fatal("patchSum field not found")
	}
	pos := idx + len(`"patchSum":"`)
	raw[pos] = toggleHex(raw[pos])
	if err := os.WriteFile(fx.patch, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	_ = s
	rr := runApply(fx, nil)
	if rr.exitCode != 11 {
		t.Fatalf("want exit 11 (corrupt patch), got %d: %s", rr.exitCode, rr.stderr)
	}
	if digestFile(t, fx.target) != fx.oldSum {
		t.Fatal("corrupt-patch attempt mutated target")
	}
}

// TestCLISpacePreflight: simulated tiny free space refuses before writing.
func TestCLISpacePreflight(t *testing.T) {
	fx := setupFixture(t)
	rr := runApply(fx, map[string]string{"DELTA_SIM_FREE_BYTES": "1"})
	if rr.exitCode != 12 {
		t.Fatalf("want exit 12 (no space), got %d: %s", rr.exitCode, rr.stderr)
	}
	if digestFile(t, fx.target) != fx.oldSum {
		t.Fatal("space refusal mutated target")
	}
	mustNotExist(t, fx.target+".delta.tmp")
}

// TestCLIInsertionRoundTripViaCLI exercises generate (CLI delta) + apply for
// the insertion/block-shift case end-to-end on the command line.
func TestCLIFullGenerateAndApply(t *testing.T) {
	fx := setupFixture(t)

	oldPath := filepath.Join(fx.dir, "old.bin")
	newPath := filepath.Join(fx.dir, "new.bin")
	if err := os.WriteFile(oldPath, fx.old, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(newPath, fx.new, 0o644); err != nil {
		t.Fatal(err)
	}
	patchPath := filepath.Join(fx.dir, "cli-patch.json")

	gen := exec.Command(binaryPath, "delta",
		"--cache", fx.cache,
		"--old", oldPath, "--new", newPath,
		"--block-size", "1024",
		"--patch", patchPath)
	gen.Env = append(os.Environ(), "GOPROXY=off")
	var gerr bytes.Buffer
	gen.Stderr = &gerr
	if err := gen.Run(); err != nil {
		t.Fatalf("CLI delta: %v\n%s", err, gerr.String())
	}

	// Reset target to old, then apply CLI-generated patch.
	if err := os.WriteFile(fx.target, fx.old, 0o644); err != nil {
		t.Fatal(err)
	}
	rr := runApply(fx, map[string]string{})
	// runApply uses fx.patch; instead run the CLI-generated patch explicitly.
	cmd := exec.Command(binaryPath, "apply", "--target", fx.target, "--patch", patchPath)
	cmd.Env = append(os.Environ(), "GOPROXY=off")
	var so, se2 bytes.Buffer
	cmd.Stdout, cmd.Stderr = &so, &se2
	if err := cmd.Run(); err != nil {
		t.Fatalf("CLI apply of generated patch: %v\n%s", err, se2.String())
	}
	if digestFile(t, fx.target) != fx.newSum {
		t.Fatal("CLI generated patch did not reproduce NewSum")
	}
	_ = rr
}

func toggleHex(c byte) byte {
	if c == '0' {
		return '1'
	}
	if c == '9' {
		return 'a'
	}
	return c - 1
}
