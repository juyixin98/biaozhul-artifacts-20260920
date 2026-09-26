package fault

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestScriptedTransport(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(ts.Close)

	tests := []struct {
		name       string
		fault      Kind
		wantErr    bool
		wantStatus int
	}{
		{"none passes through", KindNone, false, http.StatusOK},
		{"transport error", KindTransportError, true, 0},
		{"503", KindStatus503, false, http.StatusServiceUnavailable},
		{"500", KindStatus500, false, http.StatusInternalServerError},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tr := NewScriptedTransport(http.DefaultTransport, []Fault{{Kind: tc.fault}})
			req, _ := http.NewRequest(http.MethodGet, ts.URL, nil)
			resp, err := tr.RoundTrip(req)
			if tc.wantErr {
				if !errors.Is(err, ErrInjected) {
					t.Fatalf("err = %v, want ErrInjected", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected err %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.wantStatus)
			}
		})
	}
}

func TestScriptExhaustedPassesThrough(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(ts.Close)

	tr := NewScriptedTransport(http.DefaultTransport, []Fault{{Kind: KindStatus503}})
	req, _ := http.NewRequest(http.MethodGet, ts.URL, nil)

	resp1, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp1.Body.Close()
	if resp1.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("first status = %d", resp1.StatusCode)
	}

	// Second request: script exhausted, real server answers.
	resp2, err := tr.RoundTrip(req.Clone(req.Context()))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	body, _ := io.ReadAll(resp2.Body)
	if resp2.StatusCode != http.StatusOK || string(body) != "ok" {
		t.Fatalf("second = %d %q", resp2.StatusCode, body)
	}
	if tr.Consumed() != 1 {
		t.Fatalf("consumed = %d", tr.Consumed())
	}
}

func TestTruncatedBody(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "complete-body")
	}))
	t.Cleanup(ts.Close)

	tr := NewScriptedTransport(http.DefaultTransport, []Fault{{Kind: KindTruncated}})
	req, _ := http.NewRequest(http.MethodGet, ts.URL, nil)
	resp, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	_, err = io.ReadAll(resp.Body)
	if err == nil || !strings.Contains(err.Error(), "unexpected EOF") {
		t.Fatalf("read err = %v, want injected unexpected EOF", err)
	}
}
