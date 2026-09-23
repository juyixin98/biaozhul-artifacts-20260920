package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"loopmail/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(Handler(st, nil))
	t.Cleanup(srv.Close)
	return srv, st
}

func TestHealthAndEmptyList(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status %d", resp.StatusCode)
	}

	resp2, err := http.Get(srv.URL + "/messages")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("list status %d", resp2.StatusCode)
	}
	var body struct {
		Count    int               `json:"count"`
		Messages []json.RawMessage `json:"messages"`
	}
	if err := json.NewDecoder(resp2.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Count != 0 || len(body.Messages) != 0 {
		t.Fatalf("expected empty store, got %#v", body)
	}
}

func TestListGetDeleteFlow(t *testing.T) {
	srv, st := newTestServer(t)
	raw := "Subject: hello test\r\nFrom: a@example\r\n\r\nDot body:\r\n.\r\nend\r\n"
	id, err := st.Save("a@example", []string{"b@example", "c@example"}, []byte(raw), time.Now())
	if err != nil {
		t.Fatal(err)
	}

	// List
	resp, err := http.Get(srv.URL + "/messages")
	if err != nil {
		t.Fatal(err)
	}
	var list struct {
		Count    int `json:"count"`
		Messages []struct {
			ID      string   `json:"id"`
			From    string   `json:"from"`
			To      []string `json:"to"`
			Subject string   `json:"subject"`
			Size    int      `json:"size"`
		} `json:"messages"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if list.Count != 1 || list.Messages[0].Subject != "hello test" || len(list.Messages[0].To) != 2 {
		t.Fatalf("unexpected list payload: %#v", list)
	}

	// Get raw RFC822
	resp2, err := http.Get(srv.URL + "/messages/" + id)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 4096)
	n, _ := resp2.Body.Read(buf)
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK || !strings.Contains(resp2.Header.Get("Content-Type"), "message/rfc822") {
		t.Fatalf("get status/type: %d %s", resp2.StatusCode, resp2.Header.Get("Content-Type"))
	}
	if string(buf[:n]) != raw {
		t.Fatalf("raw body mismatch:\n got %q\nwant %q", buf[:n], raw)
	}

	// 404
	resp3, err := http.Get(srv.URL + "/messages/does-not-exist")
	if err != nil {
		t.Fatal(err)
	}
	resp3.Body.Close()
	if resp3.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404, got %d", resp3.StatusCode)
	}

	// Delete
	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/messages/"+id, nil)
	resp4, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp4.Body.Close()
	if resp4.StatusCode != http.StatusNoContent {
		t.Fatalf("want 204, got %d", resp4.StatusCode)
	}
}
