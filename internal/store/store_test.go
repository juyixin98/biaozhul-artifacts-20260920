package store

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/etag"
)

func testStore() *Store {
	return New(clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)))
}

func TestCreateAndConditionalUpdateHappyPath(t *testing.T) {
	s := testStore()
	snap, err := s.Create("doc", []byte("v0"))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if snap.Version != 1 || snap.Tag.Weak {
		t.Fatalf("unexpected initial snapshot: %+v", snap)
	}

	res := s.ConditionalPut("doc", []etag.ETag{snap.Tag}, false, []byte("v1"), nil)
	if !res.Committed || res.Snapshot.Version != 2 {
		t.Fatalf("matching update should commit, got %+v", res)
	}
	if res.Snapshot.Tag == snap.Tag {
		t.Fatal("ETag must change when content/version change")
	}
}

func TestConditionalPutFailuresDoNotMutate(t *testing.T) {
	s := testStore()
	snap, _ := s.Create("doc", []byte("orig"))
	stale := etag.New(1, []byte("totally-different"))

	cases := []struct {
		name   string
		key    string
		tags   []etag.ETag
		reason string
	}{
		{name: "stale tag", key: "doc", tags: []etag.ETag{stale}, reason: "precondition_failed"},
		{name: "weak tag", key: "doc", tags: []etag.ETag{{Weak: true, Opaque: snap.Tag.Opaque}}, reason: "precondition_failed"},
		{name: "missing resource", key: "ghost", tags: []etag.ETag{snap.Tag}, reason: "not_existing"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res := s.ConditionalPut(tc.key, tc.tags, false, []byte("new"), nil)
			if res.Committed || res.Reason != tc.reason {
				t.Fatalf("got %+v, want reason %q", res, tc.reason)
			}
			cur, ok := s.Get("doc")
			if !ok || cur.Version != snap.Version || cur.Tag != snap.Tag || string(cur.Content) != "orig" {
				t.Fatalf("state changed after failed precondition: %+v ok=%v", cur, ok)
			}
		})
	}
}

func TestHookFailureAbortsCommit(t *testing.T) {
	s := testStore()
	snap, _ := s.Create("doc", []byte("orig"))
	boom := errors.New("downstream down")

	res := s.ConditionalPut("doc", []etag.ETag{snap.Tag}, false, []byte("new"),
		func(int64) error { return boom })
	if res.Committed || !errors.Is(res.HookErr, boom) || res.Reason != "hook_failed" {
		t.Fatalf("got %+v", res)
	}
	cur, _ := s.Get("doc")
	if cur.Version != 1 || cur.Tag != snap.Tag || string(cur.Content) != "orig" {
		t.Fatalf("hook failure mutated state: %+v", cur)
	}
}

func TestWildcardRequiresExistence(t *testing.T) {
	s := testStore()
	if r := s.ConditionalPut("x", nil, true, []byte("b"), nil); r.Reason != "not_existing" {
		t.Fatalf("wildcard on missing key: %+v", r)
	}
	if _, err := s.Create("x", []byte("a")); err != nil {
		t.Fatalf("create: %v", err)
	}
	if r := s.ConditionalPut("x", nil, true, []byte("b"), nil); !r.Committed {
		t.Fatalf("wildcard on live key: %+v", r)
	}
	if r := s.ConditionalDelete("x", nil, true, nil); !r.Committed {
		t.Fatalf("wildcard delete: %+v", r)
	}
	if r := s.ConditionalPut("x", nil, true, []byte("c"), nil); r.Reason != "not_existing" {
		t.Fatalf("wildcard after delete must be not_existing: %+v", r)
	}
}

func TestDeleteRecreateDoesNotReuseVersion(t *testing.T) {
	s := testStore()
	v1, _ := s.Create("k", []byte("a"))
	if r := s.ConditionalDelete("k", []etag.ETag{v1.Tag}, false, nil); !r.Committed {
		t.Fatalf("delete: %+v", r)
	}
	if _, ok := s.Get("k"); ok {
		t.Fatal("deleted resource must not be readable")
	}
	v3, err := s.Create("k", []byte("b"))
	if err != nil {
		t.Fatalf("recreate: %v", err)
	}
	if v3.Version != 3 || v3.Tag == v1.Tag {
		t.Fatalf("recreate reused identity: v1=%+v v3=%+v", v1, v3)
	}
}

// TestConcurrentSameVersionExactlyOneWins is the headline acceptance property
// at the store level: N goroutines all CAS against the same base version; the
// version must end at base+1 with exactly one winner.
func TestConcurrentSameVersionExactlyOneWins(t *testing.T) {
	const n = 64
	s := testStore()
	base, _ := s.Create("doc", []byte("base"))

	var (
		wg      sync.WaitGroup
		winners int64
		mu      sync.Mutex
	)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			res := s.ConditionalPut("doc", []etag.ETag{base.Tag}, false,
				[]byte(fmt.Sprintf("writer-%d", i)), nil)
			if res.Committed {
				mu.Lock()
				winners++
				mu.Unlock()
			}
		}(i)
	}
	close(start)
	wg.Wait()

	if winners != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", winners)
	}
	cur, _ := s.Get("doc")
	if cur.Version != base.Version+1 {
		t.Fatalf("version advanced %d times, want 1", cur.Version-base.Version)
	}
}
