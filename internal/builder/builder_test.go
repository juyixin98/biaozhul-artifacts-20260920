package builder

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func newTestService(t *testing.T) *Service {
	t.Helper()
	base := t.TempDir()
	svc, err := New(Config{
		WorkDir:          filepath.Join(base, "work"),
		CacheDir:         filepath.Join(base, "cache"),
		DataDir:          filepath.Join(base, "data"),
		FixtureTimeout:   10 * time.Second,
		MaxFixtureOutput: 4096,
	})
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func writeSource(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "empty", ".keep"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "hello.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestBuildInlineAndCacheHit(t *testing.T) {
	svc := newTestService(t)

	fixtures := []Fixture{{
		Name: "generate",
		Args: []string{"sh", "-c", "echo generated > out.txt && mkdir -p nested/deep"},
	}}
	files := []InlineFile{{Path: "input.txt", Content: "data\n"}}

	rec1 := svc.Build(context.Background(), BuildRequest{Files: files, Fixtures: fixtures})
	if rec1.Status != "ok" {
		t.Fatalf("build1 status=%s err=%s", rec1.Status, rec1.Error)
	}
	rec2 := svc.Build(context.Background(), BuildRequest{Files: files, Fixtures: fixtures})
	if rec2.Status != "ok" {
		t.Fatalf("build2 status=%s err=%s", rec2.Status, rec2.Error)
	}
	if rec1.Artifact.Sha256 != rec2.Artifact.Sha256 {
		t.Fatalf("identical builds produced different artifacts:\n%s\n%s",
			rec1.Artifact.Sha256, rec2.Artifact.Sha256)
	}
	if rec1.Artifact.Path != rec2.Artifact.Path {
		t.Fatal("cache paths differ for identical content")
	}
	// Exactly one cache file exists.
	ents, _ := os.ReadDir(svc.cfg.CacheDir)
	var tarCount int
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), ".tar") {
			tarCount++
		}
	}
	if tarCount != 1 {
		t.Fatalf("expected 1 cached tar, got %d", tarCount)
	}
	// Successful work trees are cleaned up by default.
	if rec1.WorkDir != "" {
		if _, err := os.Stat(rec1.WorkDir); !os.IsNotExist(err) {
			t.Fatal("ephemeral work dir should have been removed")
		}
	}
}

func TestBuildDeterministicAcrossRunsAndMtimes(t *testing.T) {
	svc := newTestService(t)
	src := t.TempDir()
	writeSource(t, src)

	rec1 := svc.Build(context.Background(), BuildRequest{SourceDir: src})
	if rec1.Status != "ok" {
		t.Fatalf("build1: %s %s", rec1.Status, rec1.Error)
	}
	// Touch source files with wild mtimes, rebuild.
	newer := time.Unix(2_000_000_000, 0)
	if err := os.Chtimes(filepath.Join(src, "hello.txt"), newer, newer); err != nil {
		t.Fatal(err)
	}
	rec2 := svc.Build(context.Background(), BuildRequest{SourceDir: src})
	if rec1.Artifact.Sha256 != rec2.Artifact.Sha256 {
		t.Fatal("source mtime changed artifact bytes")
	}
}

func TestFixtureRunsAndRecordsOutput(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files: []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{
			{Name: "say-hello", Args: []string{"sh", "-c", "echo hello; echo oops >&2"}},
		},
	})
	if rec.Status != "ok" {
		t.Fatalf("status=%s err=%s", rec.Status, rec.Error)
	}
	if len(rec.Fixtures) != 1 || rec.Fixtures[0].ExitCode != 0 {
		t.Fatalf("fixture results wrong: %+v", rec.Fixtures)
	}
	if strings.TrimSpace(rec.Fixtures[0].Stdout) != "hello" {
		t.Fatalf("stdout=%q", rec.Fixtures[0].Stdout)
	}
	if strings.TrimSpace(rec.Fixtures[0].Stderr) != "oops" {
		t.Fatalf("stderr=%q", rec.Fixtures[0].Stderr)
	}
}

func TestFixtureFailureStopsPipeline(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files: []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{
			{Name: "ok", Args: []string{"true"}},
			{Name: "boom", Args: []string{"sh", "-c", "echo failure >&2; exit 7"}},
			{Name: "never", Args: []string{"true"}},
		},
	})
	if rec.Status != "fixture_failed" {
		t.Fatalf("status=%s", rec.Status)
	}
	if rec.Artifact != nil {
		t.Fatal("no artifact expected on fixture failure")
	}
	if len(rec.Fixtures) != 2 || rec.Fixtures[1].ExitCode != 7 {
		t.Fatalf("results=%+v", rec.Fixtures)
	}
	if !strings.Contains(rec.Fixtures[1].Stderr, "failure") {
		t.Fatalf("stderr=%q", rec.Fixtures[1].Stderr)
	}
}

func TestFixtureTimeout(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files:    []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{{Name: "sleep", Args: []string{"sleep", "5"}, TimeoutSeconds: 1}},
	})
	if rec.Status != "fixture_failed" || rec.Fixtures[0].ExitCode != 124 {
		t.Fatalf("status=%s results=%+v", rec.Status, rec.Fixtures)
	}
}

func TestUnknownBinaryFailsGracefully(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files:    []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{{Name: "missing", Args: []string{"definitely-no-such-binary-xyz"}}},
	})
	if rec.Status != "fixture_failed" || rec.Fixtures[0].ExitCode != 127 {
		t.Fatalf("status=%s results=%+v", rec.Status, rec.Fixtures)
	}
}

func TestEmptyFixtureRejected(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files:    []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{{Name: "empty"}},
	})
	if rec.Status != "fixture_failed" || rec.Fixtures[0].ExitCode != -1 {
		t.Fatalf("results=%+v", rec.Fixtures)
	}
}

func TestSourceSymlinkEscapeRejected(t *testing.T) {
	svc := newTestService(t)
	src := t.TempDir()
	if err := os.Symlink("..", filepath.Join(src, "up")); err != nil {
		t.Fatal(err)
	}
	rec := svc.Build(context.Background(), BuildRequest{SourceDir: src})
	if rec.Status != "error" || !strings.Contains(rec.Error, "escapes") {
		t.Fatalf("rec=%+v", rec)
	}
}

func TestFixtureCannotInjectEscapingSymlink(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files: []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{{
			Name: "evil",
			Args: []string{"sh", "-c", "ln -s ../../../../etc escape"},
		}},
	})
	if rec.Status != "error" || !strings.Contains(rec.Error, "escapes") {
		t.Fatalf("pack should reject symlink created by fixture, rec=%+v", rec)
	}
}

func TestInlinePathTraversalRejected(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files: []InlineFile{{Path: "../escape.txt", Content: "x"}},
	})
	if rec.Status != "error" {
		t.Fatalf("rec=%+v", rec)
	}
}

func TestWorkAndCacheAreSeparate(t *testing.T) {
	svc := newTestService(t)
	abs := func(p string) string { a, _ := filepath.Abs(p); return a }
	if abs(svc.cfg.WorkDir) == abs(svc.cfg.CacheDir) {
		t.Fatal("work and cache must be separate directories")
	}
	rec := svc.Build(context.Background(), BuildRequest{
		Files: []InlineFile{{Path: "f", Content: "x"}},
	})
	if !strings.HasPrefix(rec.Artifact.Path, abs(svc.cfg.CacheDir)) {
		t.Fatal("artifact must live in cache")
	}
}

func TestRecordRoundTripAndArtifactStream(t *testing.T) {
	svc := newTestService(t)
	rec := svc.Build(context.Background(), BuildRequest{
		Files:    []InlineFile{{Path: "f", Content: "payload\n"}},
		KeepWork: false,
	})
	got, ok := svc.Get(rec.ID)
	if !ok || got.Artifact.Sha256 != rec.Artifact.Sha256 {
		t.Fatal("record lookup failed")
	}
	f, art, err := svc.OpenArtifact(rec.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	h := sha256.New()
	var names []string
	tr := tar.NewReader(io.TeeReader(f, h))
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, hdr.Name)
	}
	if len(names) != 1 || names[0] != "f" {
		t.Fatalf("names=%v", names)
	}
	if hex.EncodeToString(h.Sum(nil)) != art.Sha256 {
		t.Fatal("streamed artifact hash mismatch")
	}
}

func TestRecordPersistedAcrossRestart(t *testing.T) {
	base := t.TempDir()
	cfg := Config{
		WorkDir:        filepath.Join(base, "work"),
		CacheDir:       filepath.Join(base, "cache"),
		DataDir:        filepath.Join(base, "data"),
		FixtureTimeout: time.Second,
	}
	svc1, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	rec := svc1.Build(context.Background(), BuildRequest{Files: []InlineFile{{Path: "f", Content: "x"}}})
	svc2, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := svc2.Get(rec.ID)
	if !ok {
		t.Fatal("record did not survive restart")
	}
	if got.Artifact == nil || got.Artifact.Sha256 != rec.Artifact.Sha256 {
		t.Fatal("persisted record mismatch")
	}
}

func TestFixtureEnvironmentIsMinimalAndDeterministic(t *testing.T) {
	svc := newTestService(t)
	t.Setenv("SECRET_TOKEN", "must-not-leak")
	rec := svc.Build(context.Background(), BuildRequest{
		Files:    []InlineFile{{Path: "a", Content: "a"}},
		Fixtures: []Fixture{{Name: "env", Args: []string{"env"}}},
	})
	if rec.Status != "ok" {
		t.Fatalf("%s %s", rec.Status, rec.Error)
	}
	if strings.Contains(rec.Fixtures[0].Stdout, "SECRET_TOKEN") {
		t.Fatal("host environment leaked into fixture")
	}
	if !strings.Contains(rec.Fixtures[0].Stdout, "TZ=UTC") {
		t.Fatalf("deterministic TZ missing:\n%s", rec.Fixtures[0].Stdout)
	}
}

func TestRecordSerializesJSON(t *testing.T) {
	r := &Record{ID: "abc", Status: "ok", Artifact: &Artifact{Sha256: "deadbeef", Size: 1024}}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(b, []byte(`"sha256":"deadbeef"`)) {
		t.Fatalf("json=%s", b)
	}
}
