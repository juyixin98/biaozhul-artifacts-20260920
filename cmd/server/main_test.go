package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTransferHandler(t *testing.T) {
	mux := http.NewServeMux()
	s := server{}
	mux.HandleFunc("/transfer", s.handleTransfer)
	srv := httptest.NewServer(mux)
	defer srv.Close()

	payload := bytes.Repeat([]byte("HTTP-handler-test-"), 200) // 3800 字节

	// 1) 干净传输
	resp, err := http.Post(srv.URL+"/transfer?mode=fake&seed=42", "application/octet-stream", bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	var d map[string]any
	raw, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatalf("bad json: %v\n%s", err, raw)
	}
	if d["ok"] != true {
		t.Fatalf("clean transfer not ok: %v", d["error"])
	}
	hs := d["hashes"].(map[string]any)
	if hs["sender"] != hs["receiver"] {
		t.Fatal("hash mismatch in clean transfer")
	}

	// 2) 固定种子、有限预算、全部故障类型
	resp2, err := http.Post(srv.URL+
		"/transfer?mode=fake&seed=20260924&loss=1&ackloss=1&dup=1&ackdup=1&reorder=1&hold=2&budget=6",
		"application/octet-stream", bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	var d2 map[string]any
	raw2, _ := io.ReadAll(resp2.Body)
	resp2.Body.Close()
	if err := json.Unmarshal(raw2, &d2); err != nil {
		t.Fatalf("bad json: %v\n%s", err, raw2)
	}
	if d2["ok"] != true {
		t.Fatalf("fault transfer not ok: %v", d2["error"])
	}
	f := d2["faults"].(map[string]any)
	ab := f["dataDirection"].(map[string]any)
	ba := f["ackDirection"].(map[string]any)
	if ab["Lost"].(float64) == 0 || ba["Lost"].(float64) == 0 {
		t.Error("expected losses with budget 6")
	}
	if ab["GhostSent"].(float64) == 0 {
		t.Error("expected ghost packets")
	}

	// 3) 非 POST 返回 405
	req, _ := http.NewRequest(http.MethodGet, srv.URL+"/transfer", nil)
	if resp3, err := http.DefaultClient.Do(req); err != nil || resp3.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET status=%v err=%v, want 405", resp3.StatusCode, err)
	}
}
