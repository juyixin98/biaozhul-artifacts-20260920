package store

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"conditionupdate/internal/clock"
	"conditionupdate/internal/etag"
)

func mustParseCond(t *testing.T, header string) *etag.Condition {
	t.Helper()
	c, err := etag.ParseCondition(header)
	if err != nil {
		t.Fatalf("parse condition %q: %v", header, err)
	}
	return &c
}

func newStore() *Store {
	return New(clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC)))
}

func create(t *testing.T, s *Store, id, data string) Resource {
	t.Helper()
	r, err := s.Put(id, Update{
		IfNoneMatch: mustParseCond(t, "*"),
		Data:        []byte(data),
	}, nil)
	if err != nil {
		t.Fatalf("create %s: %v", id, err)
	}
	return r
}

// Acceptance: two concurrent updates carrying the same version (ETag)
// must result in exactly one success.
func TestConcurrentSameVersionUpdate_OnlyOneWins(t *testing.T) {
	s := newStore()
	base := create(t, s, "doc", "v1-content")
	cond := mustParseCond(t, `"`+base.ETag+`"`)

	const racers = 2
	var wg sync.WaitGroup
	start := make(chan struct{})
	results := make([]error, racers)
	for i := 0; i < racers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, err := s.Put("doc", Update{
				IfMatch: cond,
				Data:    []byte(fmt.Sprintf("racer-%d", i)),
			}, nil)
			results[i] = err
		}(i)
	}
	close(start)
	wg.Wait()

	successes := 0
	for _, err := range results {
		if err == nil {
			successes++
		} else if !errors.Is(err, ErrPreconditionFailed) {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if successes != 1 {
		t.Fatalf("expected exactly 1 success, got %d", successes)
	}

	final, err := s.Get("doc")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if final.Version != 2 {
		t.Fatalf("expected version 2, got %d", final.Version)
	}
}

// Acceptance: after delete + recreate, the old ETag must not match the
// new resource.
func TestDeleteRecreate_OldETagRejected(t *testing.T) {
	s := newStore()
	v1 := create(t, s, "doc", "first")

	if err := s.Delete("doc", mustParseCond(t, `"`+v1.ETag+`"`)); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := s.Get("doc"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected not found after delete, got %v", err)
	}

	v2 := create(t, s, "doc", "second")
	if v2.ETag == v1.ETag {
		t.Fatal("recreated resource must not reuse the old ETag")
	}

	// Stale ETag from before the delete must fail.
	_, err := s.Put("doc", Update{
		IfMatch: mustParseCond(t, `"`+v1.ETag+`"`),
		Data:    []byte("stale write"),
	}, nil)
	if !errors.Is(err, ErrPreconditionFailed) {
		t.Fatalf("expected precondition failed for stale ETag, got %v", err)
	}

	// Correct new ETag succeeds.
	if _, err := s.Put("doc", Update{
		IfMatch: mustParseCond(t, `"`+v2.ETag+`"`),
		Data:    []byte("fresh write"),
	}, nil); err != nil {
		t.Fatalf("update with fresh ETag: %v", err)
	}
}

// Acceptance: If-Match: * matches any existing resource but not a
// missing one.
func TestWildcardIfMatch(t *testing.T) {
	s := newStore()
	create(t, s, "doc", "content")

	if _, err := s.Put("doc", Update{
		IfMatch: mustParseCond(t, "*"),
		Data:    []byte("updated"),
	}, nil); err != nil {
		t.Fatalf("If-Match: * on existing resource: %v", err)
	}

	if _, err := s.Put("ghost", Update{
		IfMatch: mustParseCond(t, "*"),
		Data:    []byte("nope"),
	}, nil); !errors.Is(err, ErrPreconditionFailed) {
		t.Fatalf("If-Match: * on missing resource must fail, got %v", err)
	}
}

// Acceptance: a request without any precondition is rejected.
func TestMissingPreconditionRejected(t *testing.T) {
	s := newStore()
	create(t, s, "doc", "content")

	if _, err := s.Put("doc", Update{Data: []byte("x")}, nil); !errors.Is(err, ErrPreconditionRequired) {
		t.Fatalf("expected ErrPreconditionRequired, got %v", err)
	}
	if err := s.Delete("doc", nil); !errors.Is(err, ErrPreconditionRequired) {
		t.Fatalf("delete without If-Match: expected ErrPreconditionRequired, got %v", err)
	}
}

// Acceptance: a weak ETag must not satisfy the strong comparison used
// by If-Match, even when the opaque value is identical.
func TestWeakETagCannotStrongMatch(t *testing.T) {
	s := newStore()
	base := create(t, s, "doc", "content")

	_, err := s.Put("doc", Update{
		IfMatch: mustParseCond(t, `W/"`+base.ETag+`"`),
		Data:    []byte("weak write"),
	}, nil)
	if !errors.Is(err, ErrPreconditionFailed) {
		t.Fatalf("weak ETag must fail strong comparison, got %v", err)
	}

	// The failed attempt must not have changed anything.
	cur, err := s.Get("doc")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if cur.ETag != base.ETag || cur.Version != 1 {
		t.Fatalf("store changed after rejected weak-ETag write: %+v", cur)
	}
}

// Acceptance: a failing BeforeCommit hook (downstream failure) aborts
// the update with zero side effects.
func TestFailedHookLeavesNoSideEffects(t *testing.T) {
	s := newStore()
	base := create(t, s, "doc", "content")

	hookErr := errors.New("downstream exploded")
	_, err := s.Put("doc", Update{
		IfMatch: mustParseCond(t, `"`+base.ETag+`"`),
		Data:    []byte("should never land"),
	}, func(Resource) error { return hookErr })
	if !errors.Is(err, hookErr) {
		t.Fatalf("expected hook error, got %v", err)
	}

	cur, err := s.Get("doc")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if cur.ETag != base.ETag || cur.Version != 1 || string(cur.Data) != "content" {
		t.Fatalf("store mutated despite failed hook: %+v", cur)
	}
}

// Version and content must change atomically: readers never observe a
// new version with old content or vice versa.
func TestVersionAndContentChangeAtomically(t *testing.T) {
	s := newStore()
	base := create(t, s, "doc", "gen-0")

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			r, err := s.Get("doc")
			if err != nil {
				continue
			}
			want := fmt.Sprintf("gen-%d", r.Version-1)
			if string(r.Data) != want {
				t.Errorf("torn read: version=%d data=%q", r.Version, r.Data)
				return
			}
		}
	}()

	cur := base
	for i := 0; i < 200; i++ {
		next, err := s.Put("doc", Update{
			IfMatch: mustParseCond(t, `"`+cur.ETag+`"`),
			Data:    []byte(fmt.Sprintf("gen-%d", i+1)),
		}, nil)
		if err != nil {
			t.Fatalf("update %d: %v", i, err)
		}
		cur = next
	}
	close(stop)
	wg.Wait()
}

func TestIfNoneMatchStarCreateOnly(t *testing.T) {
	s := newStore()
	create(t, s, "doc", "first")

	_, err := s.Put("doc", Update{
		IfNoneMatch: mustParseCond(t, "*"),
		Data:        []byte("overwrite attempt"),
	}, nil)
	if !errors.Is(err, ErrPreconditionFailed) {
		t.Fatalf("If-None-Match: * on existing resource must fail, got %v", err)
	}
}

// If-None-Match uses weak comparison: a weak tag with the same opaque
// value as the current ETag must still prevent the write.
func TestIfNoneMatchWeakComparison(t *testing.T) {
	s := newStore()
	base := create(t, s, "doc", "content")

	_, err := s.Put("doc", Update{
		IfNoneMatch: mustParseCond(t, `W/"`+base.ETag+`"`),
		Data:        []byte("blocked"),
	}, nil)
	if !errors.Is(err, ErrPreconditionFailed) {
		t.Fatalf("weak If-None-Match matching current must fail (weak comparison), got %v", err)
	}
}

// Precondition evaluation precedes existence (RFC 7232 §3.1): deleting
// a missing resource with an If-Match that cannot match is a 412, not 404.
func TestDeleteMissingWithFailingPreconditionIs412(t *testing.T) {
	s := newStore()

	for _, h := range []string{`*`, `"ghost-v1-deadbeef"`} {
		err := s.Delete("ghost", mustParseCond(t, h))
		if !errors.Is(err, ErrPreconditionFailed) {
			t.Fatalf("Delete missing with If-Match %s: expected 412, got %v", h, err)
		}
	}
}
