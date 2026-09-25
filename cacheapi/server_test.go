package cacheapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"modelcache/digest"
	"modelcache/store"
)

func newTestServer(t *testing.T, maxSize int64) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.New(store.Options{Root: t.TempDir(), MaxObjectSize: maxSize})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(NewServer(st, nil).Handler())
	t.Cleanup(ts.Close)
	return ts, st
}

func TestUploadDownloadVerify(t *testing.T) {
	ts, _ := newTestServer(t, 1<<20)
	ctx := context.Background()
	payload := bytes.Repeat([]byte("artifact-"), 500)
	dgst := digest.NewSHA256(payload)

	// Upload.
	req, _ := http.NewRequestWithContext(ctx, http.MethodPut, ts.URL+"/v1/blobs/"+dgst.String(), bytes.NewReader(payload))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("upload status %d: %s", resp.StatusCode, body)
	}
	var pr struct {
		Existed bool `json:"existed"`
		Size    int  `json:"size"`
	}
	json.NewDecoder(resp.Body).Decode(&pr)
	resp.Body.Close()
	if pr.Existed || pr.Size != len(payload) {
		t.Fatalf("unexpected put result %+v", pr)
	}

	// HEAD.
	headReq, _ := http.NewRequest(http.MethodHead, ts.URL+"/v1/blobs/"+dgst.String(), nil)
	hresp, err := http.DefaultClient.Do(headReq)
	if err != nil {
		t.Fatal(err)
	}
	if hresp.StatusCode != http.StatusOK || hresp.Header.Get("X-Content-Digest") != dgst.String() {
		t.Fatalf("bad HEAD: %+v", hresp.Header)
	}
	hresp.Body.Close()

	// GET + integrity.
	gresp, err := http.Get(ts.URL + "/v1/blobs/" + dgst.String())
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(gresp.Body)
	gresp.Body.Close()
	if !bytes.Equal(got, payload) {
		t.Fatal("download mismatch")
	}

	// Range request (download interruption recovery primitive).
	rreq, _ := http.NewRequest(http.MethodGet, ts.URL+"/v1/blobs/"+dgst.String(), nil)
	rreq.Header.Set("Range", "bytes=100-199")
	rresp, err := http.DefaultClient.Do(rreq)
	if err != nil {
		t.Fatal(err)
	}
	if rresp.StatusCode != http.StatusPartialContent {
		t.Fatalf("range status = %d, want 206", rresp.StatusCode)
	}
	rgot, _ := io.ReadAll(rresp.Body)
	rresp.Body.Close()
	if !bytes.Equal(rgot, payload[100:200]) || !strings.HasPrefix(rresp.Header.Get("Content-Range"), "bytes 100-199/") {
		t.Fatalf("bad range body/content-range: %q", rresp.Header.Get("Content-Range"))
	}
}

func TestRejectsBadDigest(t *testing.T) {
	ts, _ := newTestServer(t, 1<<20)
	good := digest.NewSHA256([]byte("x")).String()
	resp, err := http.Post(ts.URL+"/v1/blobs/"+good, "application/octet-stream", strings.NewReader("different body"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}
	var env apiError
	json.NewDecoder(resp.Body).Decode(&env)
	if env.Code != "digest_mismatch" {
		t.Fatalf("code=%q want digest_mismatch", env.Code)
	}

	// Object must not be downloadable.
	gresp, err := http.Get(ts.URL + "/v1/blobs/" + good)
	if err != nil {
		t.Fatal(err)
	}
	gresp.Body.Close()
	if gresp.StatusCode != http.StatusNotFound {
		t.Fatalf("partial/bad object visible! status=%d", gresp.StatusCode)
	}
}

func TestConcurrentSameDigestUploads(t *testing.T) {
	ts, st := newTestServer(t, 1<<20)
	payload := bytes.Repeat([]byte("race"), 8192)
	dgst := digest.NewSHA256(payload)

	const n = 24
	var wg sync.WaitGroup
	statuses := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			req, _ := http.NewRequest(http.MethodPut, ts.URL+"/v1/blobs/"+dgst.String(), bytes.NewReader(payload))
			resp, err := http.DefaultClient.Do(req)
			if err == nil {
				statuses[i] = resp.StatusCode
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
			}
		}(i)
	}
	wg.Wait()
	for i, code := range statuses {
		if code != http.StatusOK {
			t.Fatalf("upload %d status = %d", i, code)
		}
	}
	stats, err := st.Stats(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if stats.Objects != 1 || stats.TempFiles != 0 {
		t.Fatalf("stats = %+v, want exactly 1 object and 0 temps", stats)
	}
}

func TestCorruptObjectDiagnosedAndQuarantined(t *testing.T) {
	ts, st := newTestServer(t, 1<<20)
	ctx := context.Background()
	payload := []byte(strings.Repeat("abcd", 2000))
	dgst := digest.NewSHA256(payload)
	if _, _, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload)); err != nil {
		t.Fatal(err)
	}
	// Tamper with the on-disk blob directly.
	p, err := stPutPath(st, dgst)
	if err != nil {
		t.Fatal(err)
	}
	os.Chmod(p, 0o644)
	if err := os.WriteFile(p, []byte("tampered"), 0o644); err != nil {
		t.Fatal(err)
	}

	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/v1/blobs/"+dgst.String()+"/verify", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("verify status = %d body=%s", resp.StatusCode, body)
	}
	var env apiError
	json.Unmarshal(body, &env)
	if env.Code != "corrupt" || env.Extra["quarantined"] != true {
		t.Fatalf("unexpected verify envelope: %+v", env)
	}

	// Subsequent GET must 404 (cache self-healed).
	gresp, _ := http.Get(ts.URL + "/v1/blobs/" + dgst.String())
	gresp.Body.Close()
	if gresp.StatusCode != http.StatusNotFound {
		t.Fatalf("GET after quarantine status = %d, want 404", gresp.StatusCode)
	}
}

func TestRestartPersistsAndSweepsTmp(t *testing.T) {
	dir := t.TempDir()
	st1, err := store.New(store.Options{Root: dir, MaxObjectSize: 1 << 20})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte("persist-me-across-restart")
	dgst := digest.NewSHA256(payload)
	if _, _, err := st1.Put(context.Background(), dgst, -1, bytes.NewReader(payload)); err != nil {
		t.Fatal(err)
	}
	// Simulate crashed upload orphan.
	if err := os.WriteFile(filepath.Join(dir, "tmp", "upload-orphan.part"), []byte("xx"), 0o600); err != nil {
		t.Fatal(err)
	}

	// "Restart": a brand new Store over the same root.
	st2, err := store.New(store.Options{Root: dir, MaxObjectSize: 1 << 20})
	if err != nil {
		t.Fatal(err)
	}
	n, err := st2.Sweep(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("sweep n=%d err=%v", n, err)
	}
	f, _, err := st2.Get(context.Background(), dgst)
	if err != nil {
		t.Fatalf("object lost across restart: %v", err)
	}
	f.Close()
}

// stPutPath reaches the unexported digestPath via a tiny helper in-package.
func stPutPath(st *store.Store, dgst digest.Digest) (string, error) {
	return filepath.Join(st.Root(), "blobs", dgst.Algorithm(), dgst.Hex()[:2], dgst.Hex()), nil
}
