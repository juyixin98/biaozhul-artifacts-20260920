package httpapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"reproducible-archive/internal/service"
)

func newTestServer(t *testing.T) (*Server, string) {
	t.Helper()
	tmp := t.TempDir()
	src := filepath.Join(tmp, "src")
	if err := os.MkdirAll(filepath.Join(src, "空目录"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "a-文件.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(src, "a-文件.txt"), time.Unix(123, 0), time.Unix(123, 0)); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("a-文件.txt", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}

	mgr, err := service.NewManager(filepath.Join(tmp, "work"), filepath.Join(tmp, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	return NewServer(mgr), src
}

func sha256Hex(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// do 发起请求并返回状态码与解析后的 JSON 主体。
func do(t *testing.T, srv *Server, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var r io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		r = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, r)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	raw := rec.Body.Bytes()
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("响应不是 JSON: %v\n%s", err, raw)
		}
	}
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t)
	code, body := do(t, srv, http.MethodGet, "/healthz", nil)
	if code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz 异常: %d %v", code, body)
	}
}

func TestBuildLifecycle(t *testing.T) {
	srv, src := newTestServer(t)
	out := filepath.Join(t.TempDir(), "out.tar")

	// 首次构建 201。
	code, body := do(t, srv, http.MethodPost, "/v1/builds", map[string]any{
		"source_dir": src, "output_path": out,
	})
	if code != http.StatusCreated {
		t.Fatalf("首次构建状态码=%d body=%v", code, body)
	}
	id, _ := body["build_id"].(string)
	sha1, _ := body["artifact_sha256"].(string)
	if id == "" || sha1 == "" {
		t.Fatalf("响应缺少 build_id/artifact_sha256: %v", body)
	}
	if hit, _ := body["cache_hit"].(bool); hit {
		t.Fatal("首次构建 cache_hit 应为 false")
	}
	if entries, _ := body["entries"].([]any); len(entries) < 3 {
		t.Fatalf("条目数量异常: %v", body["entries"])
	}

	// 输出文件确实存在且哈希匹配。
	b, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if got := sha256Hex(b); got != sha1 {
		t.Fatalf("输出文件哈希 %s != 清单 %s", got, sha1)
	}

	// 第二次构建同样内容 -> 201 且 cache_hit=true，哈希一致。
	code, body2 := do(t, srv, http.MethodPost, "/v1/builds", map[string]any{
		"source_dir": src, "output_path": filepath.Join(t.TempDir(), "out2.tar"),
	})
	if code != http.StatusCreated {
		t.Fatalf("第二次构建状态码=%d body=%v", code, body2)
	}
	if hit, _ := body2["cache_hit"].(bool); !hit {
		t.Fatal("第二次构建 cache_hit 应为 true")
	}
	if body2["artifact_sha256"] != sha1 {
		t.Fatal("两次构建哈希不一致")
	}

	// GET 单次构建。
	code, body3 := do(t, srv, http.MethodGet, "/v1/builds/"+id, nil)
	if code != http.StatusOK || body3["build_id"] != id {
		t.Fatalf("GET 单次构建异常: %d %v", code, body3)
	}

	// GET 列表。
	code, body4 := do(t, srv, http.MethodGet, "/v1/builds", nil)
	if code != http.StatusOK {
		t.Fatalf("GET 列表状态码=%d", code)
	}
	if builds, _ := body4["builds"].([]any); len(builds) != 2 {
		t.Fatalf("列表长度=%d, 期望 2", len(builds))
	}
}

func TestNotFound(t *testing.T) {
	srv, _ := newTestServer(t)
	code, body := do(t, srv, http.MethodGet, "/v1/builds/nope", nil)
	if code != http.StatusNotFound {
		t.Fatalf("状态码=%d, 期望 404", code)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "not_found" {
		t.Fatalf("错误码异常: %v", body)
	}
}

func TestInvalidJSON(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/builds", bytes.NewReader([]byte("{not json")))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("状态码=%d, 期望 400", rec.Code)
	}
}

func TestInvalidRequestFields(t *testing.T) {
	srv, _ := newTestServer(t)
	code, body := do(t, srv, http.MethodPost, "/v1/builds", map[string]any{
		"source_dir":  "relative/path",
		"output_path": filepath.Join(t.TempDir(), "x.tar"),
	})
	if code != http.StatusBadRequest {
		t.Fatalf("状态码=%d, 期望 400, body=%v", code, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "invalid_request" {
		t.Fatalf("错误码异常: %v", body)
	}
}

func TestUnsafeSymlinkRejected(t *testing.T) {
	srv, _ := newTestServer(t)
	evil := filepath.Join(t.TempDir(), "evil-src")
	if err := os.MkdirAll(evil, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("../../../../etc/passwd", filepath.Join(evil, "link")); err != nil {
		t.Fatal(err)
	}
	code, body := do(t, srv, http.MethodPost, "/v1/builds", map[string]any{
		"source_dir":  evil,
		"output_path": filepath.Join(t.TempDir(), "x.tar"),
	})
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("状态码=%d, 期望 422, body=%v", code, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "unsafe_symlink" {
		t.Fatalf("错误码异常: %v", body)
	}
}
