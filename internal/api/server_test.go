package api

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"reprobuild/internal/builder"
)

func newTestServer(t *testing.T) (*httptest.Server, *builder.Service) {
	t.Helper()
	base := t.TempDir()
	svc, err := builder.New(builder.Config{
		WorkDir:        filepath.Join(base, "work"),
		CacheDir:       filepath.Join(base, "cache"),
		DataDir:        filepath.Join(base, "data"),
		FixtureTimeout: 5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	srv := httptest.NewServer(NewServer(svc, logger).Routes())
	t.Cleanup(srv.Close)
	return srv, svc
}

func postJSON(t *testing.T, url string, body any) (int, []byte) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/api/v1/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestBuildLifecycleOverHTTP(t *testing.T) {
	srv, _ := newTestServer(t)

	status, body := postJSON(t, srv.URL+"/api/v1/builds", map[string]any{
		"files": []map[string]string{
			{"path": "hello.txt", "content": "world\n"},
			{"path": "目录/文件.txt", "content": "unicode\n"},
		},
		"fixtures": []map[string]any{
			{"name": "add", "args": []string{"sh", "-c", "echo done > marker.txt"}},
		},
	})
	if status != 200 {
		t.Fatalf("create status=%d body=%s", status, body)
	}
	var rec builder.Record
	if err := json.Unmarshal(body, &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Status != "ok" {
		t.Fatalf("record=%s", body)
	}

	// GET record
	resp, err := http.Get(srv.URL + "/api/v1/builds/" + rec.ID)
	if err != nil {
		t.Fatal(err)
	}
	body2, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !bytes.Contains(body2, []byte(rec.ID)) {
		t.Fatalf("get build failed: %d %s", resp.StatusCode, body2)
	}

	// GET artifact: verify headers and that content is a tar
	resp, err = http.Get(srv.URL + "/api/v1/builds/" + rec.ID + "/artifact")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("artifact status=%d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/x-tar" {
		t.Fatalf("content-type=%s", ct)
	}
	if resp.Header.Get("X-Artifact-Sha256") != rec.Artifact.Sha256 {
		t.Fatal("artifact hash header mismatch")
	}
	var names []string
	tr := tar.NewReader(resp.Body)
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
	joined := strings.Join(names, ",")
	for _, want := range []string{"hello.txt", "目录/文件.txt", "marker.txt"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("archive missing %s, have %v", want, names)
		}
	}
}

func TestBuildFromSourceDir(t *testing.T) {
	srv, _ := newTestServer(t)
	src := t.TempDir()
	if err := os.MkdirAll(filepath.Join(src, "empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "empty", ".keep"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "f"), []byte("1"), 0o644); err != nil {
		t.Fatal(err)
	}
	status, body := postJSON(t, srv.URL+"/api/v1/builds", map[string]any{
		"source_dir": src,
	})
	if status != 200 {
		t.Fatalf("status=%d body=%s", status, body)
	}
	var rec builder.Record
	if err := json.Unmarshal(body, &rec); err != nil || rec.Status != "ok" {
		t.Fatalf("status=%d body=%s", status, body)
	}
}

func TestUnknownBuild(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/api/v1/builds/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestInvalidJSONRejected(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Post(srv.URL+"/api/v1/builds", "application/json", strings.NewReader("{nope"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	srv, _ := newTestServer(t)
	status, body := postJSON(t, srv.URL+"/api/v1/builds", map[string]any{
		"files":       []map[string]string{{"path": "a", "content": "b"}},
		"bogus_field": 1,
	})
	if status != 400 {
		t.Fatalf("status=%d body=%s", status, body)
	}
}

func TestFixtureFailureReflectedOverHTTP(t *testing.T) {
	srv, _ := newTestServer(t)
	status, body := postJSON(t, srv.URL+"/api/v1/builds", map[string]any{
		"files": []map[string]string{{"path": "a", "content": "b"}},
		"fixtures": []map[string]any{
			{"name": "fail", "args": []string{"sh", "-c", "exit 3"}},
		},
	})
	if status != 200 {
		t.Fatalf("http status=%d", status)
	}
	var rec builder.Record
	if err := json.Unmarshal(body, &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Status != "fixture_failed" || rec.Fixtures[0].ExitCode != 3 {
		t.Fatalf("record=%s", body)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	srv, _ := newTestServer(t)
	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/api/v1/builds", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}
