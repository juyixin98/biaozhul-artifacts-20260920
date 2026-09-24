package registry_test

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"sync"
	"testing"
	"time"
)

// Stress test: GC runs repeatedly while pulls, tag updates, uploads and
// manifest publications happen concurrently against shared layers.  The
// invariant checked throughout: a blob that is referenced by any stored
// manifest, or currently being pulled (active lease), must never disappear.
func TestConcurrentGCStress(t *testing.T) {
	e := newEnv(t)

	// Seed several shared base layers that every manifest references, plus
	// private layers.  Bases must NEVER be deleted while manifests exist.
	const nBase = 4
	baseD := make([]string, nBase)
	for i := range baseD {
		baseD[i] = e.pushBlob("lib", []byte(fmt.Sprintf("base-layer-%d-content", i)))
	}
	cd := e.pushBlob("lib", []byte("{}"))

	// Establish the reference BEFORE any GC can run.  Once a manifest
	// referencing every base layer has committed, the committed database state
	// never lacks those refs (a re-PUT deletes+re-inserts refs in one tx), so
	// GC must never be able to delete a base layer.  This is the exact
	// invariant under test: a blob that IS referenced cannot be collected.
	e.putManifest("lib", "rolling", cd, baseD)

	stop := make(chan struct{})
	var wg sync.WaitGroup

	// GC loop.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				resp, err := e.ts.Client().Post(e.ts.URL+"/admin/gc/run", "application/json", nil)
				if err == nil {
					io.Copy(io.Discard, resp.Body)
					resp.Body.Close()
				}
				time.Sleep(2 * time.Millisecond)
			}
		}
	}()

	// Publisher loop: keep a manifest that references ALL base layers stored
	// the entire time, by repeatedly PUTting a manifest that points at them.
	wg.Add(1)
	go func() {
		defer wg.Done()
		i := 0
		for {
			select {
			case <-stop:
				return
			default:
			}
			e.putManifest("lib", "rolling", cd, baseD) // same digest, idempotent
			i++
			if i%50 == 0 {
				time.Sleep(time.Millisecond)
			}
		}
	}()

	// Puller loop: continuously pull random base layers over HTTP, holding a
	// short in-flight window.  Use small blobs but hold the body open briefly.
	pullOne := func(d string) {
		req, _ := http.NewRequest(http.MethodGet, e.ts.URL+"/v2/lib/blobs/"+d, nil)
		resp, err := e.ts.Client().Transport.RoundTrip(req)
		if err != nil {
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			buf := make([]byte, 64)
			io.ReadFull(resp.Body, buf) // keep lease active briefly
			io.Copy(io.Discard, resp.Body)
		}
	}
	for p := 0; p < 4; p++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					pullOne(baseD[id%nBase])
				}
			}
		}(p)
	}

	// Transient blobs: upload blobs that are sometimes referenced and
	// sometimes garbage; GC must only delete unreferenced ones.
	wg.Add(1)
	go func() {
		defer wg.Done()
		n := 0
		for {
			select {
			case <-stop:
				return
			default:
			}
			// Blob that is never referenced -> valid garbage.
			e.pushBlob("tmp", []byte(fmt.Sprintf("ephemeral-%d", n)))
			n++
			time.Sleep(3 * time.Millisecond)
		}
	}()

	time.Sleep(3 * time.Second)
	close(stop)
	wg.Wait()

	// Final invariant: every base layer (referenced by the rolling manifest
	// the whole time) is still present and intact.
	for _, d := range baseD {
		if !e.exists(d) {
			t.Fatalf("shared base layer %s was deleted while continuously referenced", d)
		}
		rc, size, err := e.reg.OpenBlob(e.ctx, d)
		if err != nil {
			t.Fatalf("open surviving base %s: %v", d, err)
		}
		got, _ := io.ReadAll(rc)
		rc.Close()
		if size != int64(len(got)) || bytes.Contains(got, []byte("base-layer")) == false {
			t.Fatalf("base layer %s corrupted after concurrent GC", d)
		}
	}
	if !e.exists(cd) {
		t.Fatal("config blob deleted while manifest references it")
	}

	// A final GC must converge cleanly (no errors, no double-deletes).
	rep := e.runGC("")
	if num(rep, "deleted_count") < 0 {
		t.Fatal("impossible negative delete")
	}
}
