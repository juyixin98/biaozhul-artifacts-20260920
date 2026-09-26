package serverapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

func TestE2E_ValidationErrors(t *testing.T) {
	h := newHarness(t)
	cases := []struct {
		name   string
		method string
		body   string
		want   int
	}{
		{"method not allowed", http.MethodGet, "", http.StatusMethodNotAllowed},
		{"invalid json", http.MethodPost, "{not json", http.StatusBadRequest},
		{"no leaves", http.MethodPost, `{"leaves":[]}`, http.StatusBadRequest},
		{"unknown service", http.MethodPost,
			`{"leaves":[{"name":"x","service":"zzz"}]}`, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, _ := http.NewRequest(tc.method, h.url+"/process", bytes.NewBufferString(tc.body))
			req.Header.Set("Content-Type", "application/json")
			resp, err := h.cli.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_ = resp.Body.Close()
			if resp.StatusCode != tc.want {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.want)
			}
		})
	}
}

func TestE2E_DefaultServiceAndDefaultLeafName(t *testing.T) {
	h := newHarness(t)
	resp, body := h.post(ProcessRequest{Leaves: []LeafSpec{{}}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
	var rep map[string]any
	_ = json.Unmarshal(body, &rep)
	leaves, _ := rep["leaves"].([]any)
	if len(leaves) != 1 {
		t.Fatalf("leaves = %v", rep["leaves"])
	}
	first, _ := leaves[0].(map[string]any)
	if first["name"] != "leaf-1" {
		t.Fatalf("default name = %v", first["name"])
	}
}

func TestE2E_AdminAndStatsEndpoints(t *testing.T) {
	h := newHarness(t)
	resp, err := h.cli.Get(h.url + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()

	resp, err = h.cli.Get(h.url + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	var st ServiceStats
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if len(st.Services) != 2 || st.Goroutines == 0 {
		t.Fatalf("stats payload wrong: %+v", st)
	}

	// release without gate -> 400
	resp, err = h.cli.Get(h.url + "/admin/release")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("release status %d", resp.StatusCode)
	}

	// release with gate fans out to both services and returns 200
	resp, err = h.cli.Get(h.url + "/admin/release?gate=whatever")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("release status %d", resp.StatusCode)
	}

	resp, err = h.cli.Get(h.url + "/admin/close-idle-connections")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("close-idle status %d", resp.StatusCode)
	}
}
