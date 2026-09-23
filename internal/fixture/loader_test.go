package fixture

import (
	"os"
	"path/filepath"
	"testing"
)

// buildRoot creates a temp fixture root with one sample and returns its path.
func buildRoot(t *testing.T, container, instance, seq, ts, cpuStat, memEvents string) string {
	t.Helper()
	root := t.TempDir()
	sd := filepath.Join(root, "containers", container, instance, "samples", seq)
	if err := os.MkdirAll(sd, 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(name, content string) {
		if err := os.WriteFile(filepath.Join(sd, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("timestamp", ts)
	write("cpu.stat", cpuStat)
	write("memory.current", "1024")
	write("memory.max", "max")
	write("memory.events", memEvents)
	write("cpu.pressure", "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\nfull avg10=0.0 avg60=0.0 avg300=0.0 total=0\n")
	write("memory.pressure", "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\nfull avg10=0.0 avg60=0.0 avg300=0.0 total=0\n")
	return root
}

func TestLoadBasic(t *testing.T) {
	root := buildRoot(t, "web", "boot-1", "000001", "1700000000",
		"usage_usec 100\n", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
	insts, err := Load(root)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(insts) != 1 || insts[0].Container != "web" || insts[0].Instance != "boot-1" {
		t.Fatalf("unexpected instances: %+v", insts)
	}
	s := insts[0].Samples[0]
	if s.Seq != 1 || s.Timestamp != 1700000000 || s.CPU == nil || s.CPU.UsageUsec != 100 {
		t.Fatalf("bad sample: %+v", s)
	}
	if s.MemMaxSet != true || s.MemMax != nil {
		t.Fatalf("memory.max \"max\" should parse as unlimited: %+v", s)
	}
}

func TestMissingOptionalFileWarnsButLoads(t *testing.T) {
	root := buildRoot(t, "web", "b1", "000001", "100", "usage_usec 0\n",
		"low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
	sd := filepath.Join(root, "containers/web/b1/samples/000001")
	if err := os.Remove(filepath.Join(sd, "memory.current")); err != nil {
		t.Fatal(err)
	}
	insts, err := Load(root)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	s := insts[0].Samples[0]
	if s.MemCurrent != nil {
		t.Fatalf("MemCurrent should be nil, got %v", s.MemCurrent)
	}
	found := false
	for _, w := range s.Warnings {
		if w == `missing file "memory.current" in sample 000001` {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected missing-file warning, got %v", s.Warnings)
	}
}

func TestMissingTimestampFatal(t *testing.T) {
	root := buildRoot(t, "web", "b1", "000001", "100", "usage_usec 0\n",
		"low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
	if err := os.Remove(filepath.Join(root, "containers/web/b1/samples/000001/timestamp")); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(root); err == nil {
		t.Fatal("expected error for missing timestamp")
	}
}

func TestMalformedFileFatal(t *testing.T) {
	root := buildRoot(t, "web", "b1", "000001", "100", "garbage line here\n",
		"low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
	if _, err := Load(root); err == nil {
		t.Fatal("expected error for malformed cpu.stat")
	}
}

func TestSameNameDifferentInstances(t *testing.T) {
	root := buildRoot(t, "web", "boot-1", "000001", "100", "usage_usec 0\n",
		"low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
	// add a second instance with the same container name
	sd2 := filepath.Join(root, "containers/web/boot-2/samples/000001")
	if err := os.MkdirAll(sd2, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, content := range map[string]string{
		"timestamp":       "200",
		"cpu.stat":        "usage_usec 0\n",
		"memory.current":  "0",
		"memory.max":      "max",
		"memory.events":   "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
		"cpu.pressure":    "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\nfull avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
		"memory.pressure": "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\nfull avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
	} {
		if err := os.WriteFile(filepath.Join(sd2, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	insts, err := Load(root)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(insts) != 2 || insts[0].Instance != "boot-1" || insts[1].Instance != "boot-2" {
		t.Fatalf("expected two instances boot-1/boot-2, got %+v", insts)
	}
}
