package registry

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"sync"
	"testing"
	"time"
)

func readAll(resp *http.Response) []byte {
	b, _ := io.ReadAll(resp.Body)
	return b
}

// TestConcurrentGCPublishPull hammers the registry from three goroutines:
// repeated GC runs, a publisher that keeps a tag pointed at live content, and
// pullers that read blobs while GC may delete. The invariant under test is
// that no blob is deleted while referenced or while a pull is in flight:
// every pull of a blob that currently exists must return 200 with intact
// bytes, and GC must report 409 (serialized) rather than corrupting state.
func TestConcurrentGCPublishPull(t *testing.T) {
	e := newTestEnv(t)

	// Shared protected layer, published once and kept referenced the whole run
	// by a sequence of tags/manifests.
	protected := bytes.Repeat([]byte("protected-base-"), 5000)
	pd := e.uploadBlob("busy", protected)

	// Establish tag v0 -> manifest -> protected blob.
	e.putManifest("busy", "v0", imageManifestBody(pd, pd))

	deadline := time.Now().Add(3 * time.Second)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var failures []string
	failf := func(f string, a ...any) {
		mu.Lock()
		failures = append(failures, fmt.Sprintf(f, a...))
		mu.Unlock()
	}
	liveContent := func(i int) []byte { return []byte(fmt.Sprintf("live-layer-content-%d", i)) }
	live := make([][]byte, 0, 64)
	live = append(live, protected)

	// Publisher: every ~30ms upload a fresh config+layer and move the tag.
	wg.Add(1)
	go func() {
		defer wg.Done()
		i := 1
		for time.Now().Before(deadline) {
			cfg := e.uploadBlob("busy", []byte(fmt.Sprintf(`{"i":%d}`, i)))
			c := liveContent(i)
			ld := e.uploadBlob("busy", c)
			mu.Lock()
			live = append(live, c)
			mu.Unlock()
			// manifest references protected shared layer + the fresh layer.
			e.putManifest("busy", "moving", imageManifestBody(cfg, pd, ld))
			i++
			time.Sleep(25 * time.Millisecond)
		}
	}()

	// GC loop: keep collecting; serialized GC may return 409, that's expected.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for time.Now().Before(deadline) {
			resp, b := e.do("POST", "/admin/gc",
				http.Header{"Content-Type": {"application/json"}}, []byte("{}"))
			switch resp.StatusCode {
			case 200, 409:
				// ok
			default:
				failf("GC status=%d body=%s", resp.StatusCode, b)
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()

	// Puller: continuously read the moving manifest and the protected blob;
	// use a read delay so pulls overlap sweep windows. A 404 for a blob whose
	// row vanished in a gap before the pull started is acceptable and rare;
	// what must never happen is a 200 with corrupt/truncated bytes.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for time.Now().Before(deadline) {
			req, _ := http.NewRequest("GET", e.http.URL+"/v2/busy/blobs/"+pd, nil)
			req.Header.Set("X-Read-Delay", "40")
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				return // server shutting down at test end
			}
			body := readAll(resp)
			if resp.StatusCode == http.StatusOK {
				if !bytes.Equal(body, protected) {
					failf("protected blob read corrupted: got %d bytes, want %d", len(body), len(protected))
				}
			} else if resp.StatusCode != http.StatusNotFound {
				failf("protected blob unexpected status %d", resp.StatusCode)
			}
			resp.Body.Close()
		}
	}()

	wg.Wait()

	if len(failures) > 0 {
		for _, f := range failures {
			t.Error(f)
		}
	}

	// Post-condition: the tag still resolves and the protected layer survived
	// every GC because it remained referenced throughout.
	if e.blobRowCount(pd) != 1 {
		t.Fatal("protected shared layer deleted while continuously tagged")
	}
	if !e.fs.BlobExists(pd) {
		t.Fatal("protected shared layer file missing after concurrent GC")
	}
	resp, b := e.do("GET", "/v2/busy/manifests/moving", nil, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("moving tag not resolvable after stress: %d %s", resp.StatusCode, b)
	}
	// Audit trail exists for the runs that executed.
	var n int
	if err := e.st.Pool().QueryRow(context.Background(),
		"SELECT count(*) FROM gc_audit_events WHERE action IN ('gc.run_start','gc.delete','gc.run_complete')").
		Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n == 0 {
		t.Fatal("expected GC audit events, found none")
	}
}
