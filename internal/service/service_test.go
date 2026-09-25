package service

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"depsolver/internal/solver"
)

func diamondBody(t *testing.T, includePre bool) map[string]any {
	t.Helper()
	return map[string]any{
		"roots":              []map[string]string{{"name": "root", "constraint": "1.0.0"}},
		"includePrereleases": includePre,
		"registry": map[string]any{
			"packages": []map[string]any{
				{"name": "root", "versions": []map[string]any{
					{"version": "1.0.0", "deps": []map[string]string{
						{"name": "left", "constraint": "^1.0.0"},
						{"name": "right", "constraint": "^1.0.0"},
					}},
				}},
				{"name": "left", "versions": []map[string]any{
					{"version": "1.2.0", "deps": []map[string]string{{"name": "common", "constraint": "^2.0.0"}}},
					{"version": "1.1.0", "deps": []map[string]string{{"name": "common", "constraint": "^1.0.0"}}},
				}},
				{"name": "right", "versions": []map[string]any{
					{"version": "1.0.0", "deps": []map[string]string{{"name": "common", "constraint": "~1.3.0"}}},
				}},
				{"name": "common", "versions": []map[string]any{
					{"version": "1.3.0"}, {"version": "1.3.5"}, {"version": "2.0.0"},
				}},
			},
		},
	}
}

func do(t *testing.T, h http.Handler, method, target string, body any) (int, map[string]any) {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, target, &buf)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-JSON response: %s", rec.Body.String())
		}
	}
	return rec.Code, out
}

func TestHealthz(t *testing.T) {
	h := NewHandler()
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "GET", "/healthz", nil)
	if code != 200 || out["status"] != "ok" {
		t.Fatalf("healthz: code=%d body=%v", code, out)
	}
}

func TestResolveSAT(t *testing.T) {
	h := NewHandler()
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "POST", "/v1/resolve", diamondBody(t, false))
	if code != 200 {
		t.Fatalf("expected 200, got %d: %v", code, out)
	}
	res := decodeResult(t, out)
	if !res.Satisfiable {
		t.Fatalf("expected SAT: %v", out)
	}
	sel := map[string]string{}
	for _, s := range res.Selections {
		sel[s.Package] = s.Version
	}
	if sel["left"] != "1.1.0" || sel["common"] != "1.3.5" {
		t.Fatalf("unexpected selections: %v", sel)
	}
	if res.Stats.Backtracks == 0 {
		t.Fatal("expected backtracking recorded")
	}
}

func TestResolveConflictStatus(t *testing.T) {
	body := diamondBody(t, false)
	// 把 left 1.1.0 也改成要求 common v2 -> 无解
	pkgs := body["registry"].(map[string]any)["packages"].([]map[string]any)
	leftVers := pkgs[1]["versions"].([]map[string]any)
	leftVers[1]["deps"] = []map[string]string{{"name": "common", "constraint": ">=2.0.0"}}

	h := NewHandler()
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "POST", "/v1/resolve", body)
	if code != http.StatusConflict {
		t.Fatalf("expected 409, got %d: %v", code, out)
	}
	if _, ok := out["conflict"]; !ok {
		t.Fatalf("expected conflict body: %v", out)
	}
}

func TestResolveBadJSONAndValidation(t *testing.T) {
	h := NewHandler()
	hx := h.Routes(http.NewServeMux())
	// 未知字段被严格模式拒绝
	code, _ := do(t, hx, "POST", "/v1/resolve", map[string]any{"roots": []any{}, "bogus": 1})
	if code != 400 {
		t.Fatalf("unknown field: expected 400 got %d", code)
	}
	// roots 为空
	code, _ = do(t, hx, "POST", "/v1/resolve", map[string]any{"roots": []any{}})
	if code != 400 {
		t.Fatalf("empty roots: expected 400 got %d", code)
	}
	// 非法版本
	bad := map[string]any{
		"roots": []map[string]string{{"name": "a", "constraint": "1.0.0"}},
		"registry": map[string]any{"packages": []map[string]any{
			{"name": "a", "versions": []map[string]any{{"version": "not-a-version"}}},
		}},
	}
	code, out := do(t, hx, "POST", "/v1/resolve", bad)
	if code != 400 {
		t.Fatalf("bad version: expected 400 got %d", code)
	}
	if !strings.Contains(out["error"].(map[string]any)["message"].(string), "invalid version") {
		t.Fatalf("error should mention invalid version: %v", out)
	}
}

func TestBuildPlanWritesSeparatedDirs(t *testing.T) {
	workspace := t.TempDir()
	cache := t.TempDir()
	body := diamondBody(t, false)
	body["workspaceDir"] = workspace
	body["cacheDir"] = cache

	h := NewHandler()
	h.PlanWriter = LocalPlanWriter{}
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "POST", "/v1/build/plan", body)
	if code != 200 {
		t.Fatalf("expected 200 got %d: %v", code, out)
	}
	written := out["written"].(map[string]any)
	lockPath := written["lockfilePath"].(string)
	cachePath := written["cacheRecord"].(string)
	if filepath.Dir(lockPath) != workspace {
		t.Fatalf("lockfile must land in workspace: %s", lockPath)
	}
	if filepath.Dir(cachePath) != cache {
		t.Fatalf("cache record must land in cache dir: %s", cachePath)
	}
	lockData, err := os.ReadFile(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(lockData), "depsolver.lock/v1") ||
		!strings.Contains(string(lockData), `"version": "1.3.5"`) {
		t.Fatalf("lockfile content unexpected: %s", lockData)
	}
	// 缓存记录存在且是缓存 schema
	cacheData, err := os.ReadFile(cachePath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(cacheData), "depsolver.cache/v1") {
		t.Fatalf("cache record unexpected: %s", cacheData)
	}
}

func TestBuildPlanRejectsRelativeDirs(t *testing.T) {
	body := diamondBody(t, false)
	body["workspaceDir"] = "relative/path"
	body["cacheDir"] = t.TempDir()
	h := NewHandler()
	h.PlanWriter = LocalPlanWriter{}
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "POST", "/v1/build/plan", body)
	if code != 400 {
		t.Fatalf("expected 400 got %d: %v", code, out)
	}
}

func TestBuildPlanConflictProducesNoFiles(t *testing.T) {
	body := diamondBody(t, false)
	pkgs := body["registry"].(map[string]any)["packages"].([]map[string]any)
	pkgs[1]["versions"].([]map[string]any)[1]["deps"] =
		[]map[string]string{{"name": "common", "constraint": ">=2.0.0"}}
	body["workspaceDir"] = t.TempDir()
	body["cacheDir"] = t.TempDir()

	h := NewHandler()
	h.PlanWriter = LocalPlanWriter{}
	hx := h.Routes(http.NewServeMux())
	code, out := do(t, hx, "POST", "/v1/build/plan", body)
	if code != http.StatusConflict {
		t.Fatalf("expected 409 got %d", code)
	}
	if _, ok := out["written"]; ok {
		t.Fatal("unsat plan must not report written files")
	}
	if entries, _ := os.ReadDir(body["workspaceDir"].(string)); len(entries) != 0 {
		t.Fatal("workspace must stay empty on conflict")
	}
}

// TestPlanDeterministic：同样请求连续两次落盘，锁文件字节一致（无时间戳）。
func TestPlanDeterministic(t *testing.T) {
	h := NewHandler()
	h.PlanWriter = LocalPlanWriter{}
	hx := h.Routes(http.NewServeMux())
	w1, w2 := t.TempDir(), t.TempDir()
	c1, c2 := t.TempDir(), t.TempDir()
	run := func(wd, cd string) []byte {
		b := diamondBody(t, false)
		b["workspaceDir"] = wd
		b["cacheDir"] = cd
		req := httptest.NewRequest("POST", "/v1/build/plan", jsonReader(b))
		rec := httptest.NewRecorder()
		hx.ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("code %d: %s", rec.Code, rec.Body.String())
		}
		data, err := os.ReadFile(filepath.Join(wd, "depsolver.lock.json"))
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	if d1, d2 := run(w1, c1), run(w2, c2); !bytes.Equal(d1, d2) {
		t.Fatalf("lockfile not deterministic:\n%s\n----\n%s", d1, d2)
	}
}

func jsonReader(v any) *bytes.Buffer {
	var b bytes.Buffer
	_ = json.NewEncoder(&b).Encode(v)
	return &b
}

func decodeResult(t *testing.T, out map[string]any) solver.Result {
	t.Helper()
	raw, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	var res solver.Result
	if err := json.Unmarshal(raw, &res); err != nil {
		t.Fatal(err)
	}
	return res
}
