package worker_test

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"

	"snapshotcontroller/internal/digest"
	"snapshotcontroller/internal/worker"
)

// fakeKubectl is a stub kubectl that records the applied manifest on disk.
const fakeKubectl = `#!/usr/bin/env bash
set -euo pipefail
cat > "$FAKE_KUBECTL_CAPTURE/apply.yaml"
echo "fake kubectl: $*" >> "$FAKE_KUBECTL_CAPTURE/log"
`

type workerResult struct {
	Digest     string
	FileCount  string
	TotalBytes string
	Manifest   string
	Generation string
}

func runWorker(t *testing.T, files map[string]string, subPath string) (workerResult, int) {
	t.Helper()

	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not available")
	}
	if _, err := exec.LookPath("sha256sum"); err != nil {
		t.Skip("sha256sum not available")
	}

	root := t.TempDir()
	dataDir := filepath.Join(root, "data")
	if err := os.Mkdir(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, content := range files {
		p := filepath.Join(dataDir, name)
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// Reproduce a real ConfigMap volume mount: a timestamped data directory,
	// a "..data" symlink to it (target is a DIRECTORY) and per-key symlinks
	// through ..data. The worker must hash the keys but not the internals.
	tsDir := filepath.Join(dataDir, "..2024_01_01_000000.000000000")
	if err := os.Mkdir(tsDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(tsDir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("..2024_01_01_000000.000000000", filepath.Join(dataDir, "..data")); err != nil {
		t.Fatal(err)
	}
	for name := range files {
		_ = os.Remove(filepath.Join(dataDir, name))
		if err := os.Symlink(filepath.Join("..data", name), filepath.Join(dataDir, name)); err != nil {
			t.Fatal(err)
		}
	}
	binDir := filepath.Join(root, "bin")
	if err := os.Mkdir(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(binDir, "kubectl"), []byte(fakeKubectl), 0o755); err != nil {
		t.Fatal(err)
	}

	scriptPath := filepath.Join(root, "worker.sh")
	if err := os.WriteFile(scriptPath, []byte(worker.Script), 0o755); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command("bash", scriptPath)
	cmd.Env = append(os.Environ(),
		"PATH="+binDir+string(os.PathListSeparator)+os.Getenv("PATH"),
		"FAKE_KUBECTL_CAPTURE="+root,
		"DATA_DIR="+dataDir,
		"SNAPSHOT_NAME=mysnap",
		"SNAPSHOT_NAMESPACE=default",
		"SNAPSHOT_GENERATION=7",
		"SNAPSHOT_UID=uid-abc",
		"SOURCE_CONFIGMAP=src",
		"SUB_PATH="+subPath,
		"RESULT_CONFIGMAP=snap-mysnap-7",
	)
	out, err := cmd.CombinedOutput()
	exitCode := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			exitCode = ee.ExitCode()
		} else {
			t.Fatalf("worker failed to run: %v\n%s", err, out)
		}
	}
	t.Logf("worker output:\n%s", out)

	if exitCode != 0 {
		return workerResult{}, exitCode
	}

	applied, err := os.ReadFile(filepath.Join(root, "apply.yaml"))
	if err != nil {
		t.Fatalf("captured apply manifest: %v\n%s", err, out)
	}
	var cm struct {
		Data map[string]string `yaml:"data"`
	}
	if err := yaml.Unmarshal(applied, &cm); err != nil {
		t.Fatalf("parse applied ConfigMap: %v\n%s", err, applied)
	}
	return workerResult{
		Digest:     cm.Data["digest"],
		FileCount:  cm.Data["fileCount"],
		TotalBytes: cm.Data["totalBytes"],
		Manifest:   cm.Data["manifest"],
		Generation: cm.Data["generation"],
	}, exitCode
}

func TestWorkerMatchesGoDigest(t *testing.T) {
	content := map[string]string{"a.txt": "first", "b.txt": "second", "c.bin": string([]byte{0x00, 0x01, 0x02, 0xff})}
	res, code := runWorker(t, content, "")
	if code != 0 {
		t.Fatalf("worker exited %d", code)
	}

	blobs := digest.Files{}
	var total int
	for k, v := range content {
		blobs[k] = []byte(v)
		total += len(v)
	}
	want, err := digest.Compute(blobs, "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Digest != want.Digest {
		t.Fatalf("shell worker digest %s != Go digest %s\nmanifest:\n%s", res.Digest, want.Digest, res.Manifest)
	}
	if res.FileCount != "3" {
		t.Fatalf("fileCount=%s want 3", res.FileCount)
	}
	if res.TotalBytes != fmt.Sprintf("%d", total) {
		t.Fatalf("totalBytes=%s want %d", res.TotalBytes, total)
	}
	if res.Generation != "7" {
		t.Fatalf("generation=%s want 7", res.Generation)
	}
	// Manifest must round-trip through the Go verifier too.
	recomputed, err := digest.Verify(res.Manifest, blobs)
	if err != nil {
		t.Fatalf("worker manifest does not verify: %v", err)
	}
	if recomputed != res.Digest {
		t.Fatalf("worker manifest root mismatch: %s vs %s", recomputed, res.Digest)
	}
}

func TestWorkerSubPath(t *testing.T) {
	content := map[string]string{"a.txt": "first", "b.txt": "second"}
	res, code := runWorker(t, content, "a.txt")
	if code != 0 {
		t.Fatalf("worker exited %d", code)
	}
	want, err := digest.Compute(digest.Files{"a.txt": []byte("first")}, "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Digest != want.Digest {
		t.Fatalf("subPath shell digest %s != Go digest %s", res.Digest, want.Digest)
	}
}

func TestWorkerMissingSubPathFails(t *testing.T) {
	res, code := runWorker(t, map[string]string{"a.txt": "first"}, "nope.txt")
	if code != 2 {
		t.Fatalf("expected exit code 2 for missing file, got %d (%+v)", code, res)
	}
}

func TestWorkerScriptSyntax(t *testing.T) {
	out, err := exec.Command("bash", "-n", "-c", worker.Script).CombinedOutput()
	if err != nil {
		t.Fatalf("worker.sh syntax error: %v\n%s", err, out)
	}
	if !strings.Contains(worker.Script, "sha256sum") {
		t.Fatal("worker script must perform a real sha256sum")
	}
}
