package demo_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"sitevitals/internal/demo"
)

func TestDemoEndpoints(t *testing.T) {
	srv := httptest.NewServer(demo.NewServer())
	defer srv.Close()

	cases := []struct {
		path       string
		wantStatus int
	}{
		{"/", 200},
		{"/normal", 200},
		{"/slow", 200},
		{"/cls", 200},
		{"/longtask", 200},
		{"/partial", 200},
		{"/static/missing.png", 404},
		{"/static/broken", 500},
	}
	for _, c := range cases {
		resp, err := http.Get(srv.URL + c.path)
		if err != nil {
			t.Fatalf("%s: %v", c.path, err)
		}
		if resp.StatusCode != c.wantStatus {
			t.Errorf("%s status = %d, want %d", c.path, resp.StatusCode, c.wantStatus)
		}
		_ = resp.Body.Close()
	}

	// Two-hop in-whitelist chain.
	client := &http.Client{
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 {
				return http.ErrUseLastResponse
			}
			return nil
		},
	}
	resp, err := client.Get(srv.URL + "/redirect/1")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != 200 || resp.Request.URL.Path != "/redirect/final" {
		t.Fatalf("redirect chain ended at %s status %d", resp.Request.URL.Path, resp.StatusCode)
	}

	// Negative redirects must really issue the bad Location verbatim.
	for _, p := range []string{"/badredirect-file", "/badredirect-offsite"} {
		resp, err := http.Get(srv.URL + p)
		if err == nil {
			_ = resp.Body.Close()
			t.Fatalf("%s expected client error", p)
		}
	}
}
