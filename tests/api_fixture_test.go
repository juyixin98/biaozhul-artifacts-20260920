package tests

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"licensejudge/internal/cache"
	"licensejudge/internal/server"
)

// APICase 是 api_cases.json 的单条 HTTP 夹具。
type APICase struct {
	Name                  string         `json:"name"`
	Method                string         `json:"method"`
	Path                  string         `json:"path"`
	Body                  map[string]any `json:"body,omitempty"`
	RawBody               string         `json:"raw_body,omitempty"`
	WantStatus            int            `json:"want_status"`
	WantBodyContains      string         `json:"want_body_contains,omitempty"`
	WantVerdict           string         `json:"want_verdict,omitempty"`
	WantSelection         string         `json:"want_selection,omitempty"`
	WantAlternativesCount int            `json:"want_alternatives_count,omitempty"`
	WantErrorCode         string         `json:"want_error_code,omitempty"`
	WantCached            bool           `json:"want_cached,omitempty"`
	SendTwice             bool           `json:"send_twice,omitempty"`
}

func newFixtureServer(t *testing.T) http.Handler {
	t.Helper()
	c, err := cache.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Config{
		Policy:  loadExamplePolicy(t),
		Cache:   c,
		WorkDir: t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	return srv.Routes()
}

func TestAPIFixtures(t *testing.T) {
	var cases []APICase
	loadFixtures(t, "api_cases.json", &cases)
	if len(cases) == 0 {
		t.Fatal("api_cases.json 为空，夹具必须显式提供用例")
	}

	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			h := newFixtureServer(t)
			rec := hServe(t, h, c)
			if c.SendTwice {
				rec = hServe(t, h, c)
			}
			if rec.Code != c.WantStatus {
				t.Fatalf("HTTP 状态=%d, 期望 %d；body=%s", rec.Code, c.WantStatus, rec.Body.String())
			}
			body := rec.Body.Bytes()
			if c.WantBodyContains != "" && !bytes.Contains(body, []byte(c.WantBodyContains)) {
				t.Fatalf("响应 %s 不包含 %q", body, c.WantBodyContains)
			}
			if c.WantErrorCode != "" {
				var errResp struct {
					Code string `json:"code"`
				}
				if err := json.Unmarshal(body, &errResp); err != nil {
					t.Fatalf("错误响应非 JSON: %v (%s)", err, body)
				}
				if errResp.Code != c.WantErrorCode {
					t.Fatalf("错误码=%q, 期望 %q", errResp.Code, c.WantErrorCode)
				}
			}
			if c.WantVerdict != "" || c.WantSelection != "" || c.WantAlternativesCount > 0 || c.WantCached {
				var ok struct {
					Cached bool `json:"cached"`
					Result struct {
						Verdict      string `json:"verdict"`
						Selection    string `json:"selection"`
						Alternatives []struct {
							Expression string `json:"expression"`
						} `json:"alternatives"`
					} `json:"result"`
				}
				if err := json.Unmarshal(body, &ok); err != nil {
					t.Fatalf("成功响应非 JSON: %v (%s)", err, body)
				}
				if c.WantVerdict != "" && ok.Result.Verdict != c.WantVerdict {
					t.Fatalf("verdict=%q, 期望 %q", ok.Result.Verdict, c.WantVerdict)
				}
				if c.WantSelection != "" && ok.Result.Selection != c.WantSelection {
					t.Fatalf("selection=%q, 期望 %q", ok.Result.Selection, c.WantSelection)
				}
				if c.WantAlternativesCount > 0 && len(ok.Result.Alternatives) != c.WantAlternativesCount {
					t.Fatalf("alternatives 数量=%d, 期望 %d", len(ok.Result.Alternatives), c.WantAlternativesCount)
				}
				if c.WantCached && !ok.Cached {
					t.Fatal("期望第二次请求命中 AST 缓存 (cached=true)")
				}
			}
		})
	}
}

func hServe(t *testing.T, h http.Handler, c APICase) *httptest.ResponseRecorder {
	t.Helper()
	var bodyReader *bytes.Reader
	if c.RawBody != "" {
		bodyReader = bytes.NewReader([]byte(c.RawBody))
	} else if c.Body != nil {
		raw, err := json.Marshal(c.Body)
		if err != nil {
			t.Fatal(err)
		}
		bodyReader = bytes.NewReader(raw)
	} else {
		bodyReader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(c.Method, c.Path, bodyReader)
	if c.Body != nil || c.RawBody != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}
