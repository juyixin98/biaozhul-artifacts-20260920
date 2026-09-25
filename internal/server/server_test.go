package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"testing"

	"logcluster/internal/cluster"
)

func TestIngestAndQuery(t *testing.T) {
	srv := New(cluster.New(cluster.Config{}), "")
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	post := func(body string) map[string]any {
		resp, err := http.Post(ts.URL+"/v1/logs", "application/json", bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status %d body %s", resp.StatusCode, resp.Status)
		}
		var out map[string]any
		json.NewDecoder(resp.Body).Decode(&out)
		return out
	}

	out := post(`{"lines":["user logged in 1","user logged in 2","disk read fail 9"]}`)
	if ingested, _ := out["ingested"].(float64); ingested != 3 {
		t.Fatalf("ingested = %v, want 3", out["ingested"])
	}
	results := out["results"].([]any)
	first := results[0].(map[string]any)
	second := results[1].(map[string]any)
	third := results[2].(map[string]any)
	if first["template_id"] != second["template_id"] {
		t.Fatal("number variation should share a template")
	}
	if first["template_id"] == third["template_id"] {
		t.Fatal("different keywords must not merge")
	}

	// GET /v1/templates
	resp, _ := http.Get(ts.URL + "/v1/templates")
	var list map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if n := len(list["templates"].([]any)); n != 2 {
		t.Fatalf("want 2 templates, got %d", n)
	}

	// GET /v1/templates/{id}
	id := int64(first["template_id"].(float64))
	resp, _ = http.Get(ts.URL + "/v1/templates/" + strconv.FormatInt(id, 10))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("get template status %d", resp.StatusCode)
	}
	resp.Body.Close()

	// 404 for missing id.
	resp, _ = http.Get(ts.URL + "/v1/templates/9999")
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404, got %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestIngestValidation(t *testing.T) {
	srv := New(cluster.New(cluster.Config{}), "")
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	cases := []struct {
		body string
		code int
	}{
		{`not json`, http.StatusBadRequest},
		{`{"lines":[]}`, http.StatusBadRequest},
	}
	for _, c := range cases {
		resp, err := http.Post(ts.URL+"/v1/logs", "application/json", bytes.NewBufferString(c.body))
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != c.code {
			t.Fatalf("body %s: status %d want %d", c.body, resp.StatusCode, c.code)
		}
		resp.Body.Close()
	}
}

func TestSnapshotEndpoints(t *testing.T) {
	// No persistence configured -> 409.
	srv := New(cluster.New(cluster.Config{}), "")
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	resp, _ := http.Post(ts.URL+"/v1/snapshot", "application/json", nil)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("want 409, got %d", resp.StatusCode)
	}
	resp.Body.Close()

	// With a data file: save, restart, state survives.
	path := filepath.Join(t.TempDir(), "state.json")
	cl, err := LoadOrNew(cluster.Config{}, path)
	if err != nil {
		t.Fatal(err)
	}
	srv2 := New(cl, path)
	ts2 := httptest.NewServer(srv2.Handler())
	defer ts2.Close()
	http.Post(ts2.URL+"/v1/logs", "application/json",
		bytes.NewBufferString(`{"lines":["persist me 1","persist me 2"]}`))
	resp, _ = http.Post(ts2.URL+"/v1/snapshot", "application/json", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("snapshot save status %d", resp.StatusCode)
	}
	resp.Body.Close()

	cl2, err := LoadOrNew(cluster.Config{}, path)
	if err != nil {
		t.Fatal(err)
	}
	if cl2.Stats().Ingested != 2 || cl2.Stats().Templates != 1 {
		t.Fatalf("restored stats wrong: %+v", cl2.Stats())
	}
}

func TestHealth(t *testing.T) {
	srv := New(cluster.New(cluster.Config{}), "")
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	resp.Body.Close()
}
