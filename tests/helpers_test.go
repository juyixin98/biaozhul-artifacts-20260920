package tests

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func (e *testEnv) do(t *testing.T, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.srv.URL+path, rdr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (e *testEnv) mustStatus(t *testing.T, got, want int, body map[string]any) {
	t.Helper()
	if got != want {
		t.Fatalf("status = %d, want %d; body=%v", got, want, body)
	}
}

type batchEvent struct {
	EventID    string `json:"event_id"`
	Source     string `json:"source,omitempty"`
	DbUser     string `json:"db_user"`
	OccurredAt string `json:"occurred_at"`
	Action     string `json:"action"`
	Schema     string `json:"schema,omitempty"`
	Table      string `json:"table,omitempty"`
	RowCount   int64  `json:"row_count"`
}

func mkEvents(prefix, user string, start time.Time, n int, stepSec int) []batchEvent {
	out := make([]batchEvent, n)
	for i := 0; i < n; i++ {
		out[i] = batchEvent{
			EventID:    fmt.Sprintf("%s-%d", prefix, i),
			DbUser:     user,
			OccurredAt: start.Add(time.Duration(i*stepSec) * time.Second).Format(time.RFC3339),
			Action:     "select",
			Table:      "orders",
			RowCount:   1,
		}
	}
	return out
}

// uniqueSlug gives each test an isolated org.
func uniqueSlug(name string) string {
	return fmt.Sprintf("t-%s-%d", name, time.Now().UnixNano()%1_000_000_000)
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func newJSONPost(url string, body []byte, token string) *http.Request {
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		panic(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	return req
}

func decodeBody(resp *http.Response) map[string]any {
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return out
}

func intOr(v any) int {
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return 0
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asSlice(v any) []any {
	s, _ := v.([]any)
	return s
}

func timeNowNano() int64 { return time.Now().UnixNano() }

func hash256(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func readAllString(r io.Reader) string {
	b, _ := io.ReadAll(r)
	return string(b)
}

func indexOf(s, sub string) int { return strings.Index(s, sub) }
