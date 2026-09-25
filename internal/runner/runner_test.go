package runner

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func newTestRunner(t *testing.T, cmds []Command) (*Runner, string, string) {
	t.Helper()
	base := t.TempDir()
	work := filepath.Join(base, "work")
	cache := filepath.Join(base, "cache")
	m := &Manifest{Commands: cmds}
	r, err := NewRunner(m, base, work, cache, 5_000_000_000)
	if err != nil {
		t.Fatal(err)
	}
	return r, work, cache
}

func mustLookPath(t *testing.T, program string) string {
	t.Helper()
	p, err := exec.LookPath(program)
	if err != nil {
		t.Skipf("%q not found on PATH", program)
	}
	return p
}

func TestRunWhitelistedAndCache(t *testing.T) {
	r, work, cache := newTestRunner(t, []Command{
		{Name: "ok", Program: "true", resolved: mustLookPath(t, "true")},
	})

	// First run executes in the isolated work dir.
	res, err := r.Run(context.Background(), "ok", true)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 {
		t.Fatalf("exit = %d, stderr=%s", res.ExitCode, res.Stderr)
	}
	if res.Cached {
		t.Error("first run should not be cached")
	}
	if r.WorkDir() == r.CacheDir() {
		t.Error("work and cache dirs must differ")
	}
	if _, err := os.Stat(work); err != nil {
		t.Errorf("work dir missing: %v", err)
	}
	if entries, _ := os.ReadDir(cache); len(entries) != 1 {
		t.Errorf("expected 1 cache file, got %d", len(entries))
	}

	// Second run is served from cache.
	res2, err := r.Run(context.Background(), "ok", true)
	if err != nil {
		t.Fatal(err)
	}
	if !res2.Cached {
		t.Error("second run should be cached")
	}
}

func TestFailedCommandNotCached(t *testing.T) {
	r, _, cache := newTestRunner(t, []Command{
		{Name: "fail", Program: "false", resolved: mustLookPath(t, "false")},
	})
	res, err := r.Run(context.Background(), "fail", true)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode == 0 {
		t.Fatal("false should exit non-zero")
	}
	if entries, _ := os.ReadDir(cache); len(entries) != 0 {
		t.Fatalf("failed runs must not be cached, found %d entries", len(entries))
	}
}

func TestUnknownCommandRejected(t *testing.T) {
	r, _, _ := newTestRunner(t, []Command{{Name: "ok", Program: "true", resolved: mustLookPath(t, "true")}})
	if _, err := r.Run(context.Background(), "rm-rf-slash", true); err == nil {
		t.Fatal("non-whitelisted command must be rejected")
	}
}

func TestWorkDirEscapesBase(t *testing.T) {
	base := t.TempDir()
	r, err := NewRunner(&Manifest{Commands: []Command{
		{Name: "x", Program: "true", WorkDir: "..", resolved: mustLookPath(t, "true")},
	}}, base, filepath.Join(base, "w"), filepath.Join(base, "c"), 5_000_000_000)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := r.Run(context.Background(), "x", false); err == nil {
		t.Fatal("workdir escaping base must be rejected")
	}
}

func TestSameWorkAndCacheRejected(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "same")
	if _, err := NewRunner(&Manifest{}, base, dir, dir, 0); err == nil {
		t.Fatal("identical work/cache dirs must be rejected")
	}
}
