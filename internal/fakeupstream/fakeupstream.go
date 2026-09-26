// Package fakeupstream is an in-process fake of an external HTTP dependency.
//
// Its behavior is fully controllable: every call can succeed, fail with 5xx,
// return a transport-style error, or hang until the test explicitly releases
// it. Hung calls are the primitive used to reproduce late results: a call can
// be held while the breaker changes generations underneath it and released
// with a failure afterwards.
package fakeupstream

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"cbhalfopen/internal/vclock"
)

// Mode controls how the fake answers calls.
type Mode string

// Supported modes.
const (
	// ModeOK answers 200 immediately.
	ModeOK Mode = "ok"
	// ModeFail answers 500 immediately.
	ModeFail Mode = "fail"
	// ModeError fails the call without an HTTP response (transport error).
	ModeError Mode = "error"
	// ModeHang blocks until Release/ReleaseAll is called, then answers
	// with the mode handed to Release.
	ModeHang Mode = "hang"
)

// Attempt is one recorded call against the fake.
type Attempt struct {
	ID         uint64    `json:"id"`
	Start      time.Time `json:"start"`
	End        time.Time `json:"end,omitempty"`
	Mode       Mode      `json:"mode"`
	ReleasedAs Mode      `json:"released_as,omitempty"`
	StatusCode int       `json:"status_code"`
}

// ErrInjected is returned in ModeError and by injected client-side faults.
var ErrInjected = errors.New("fakeupstream: injected transport error (connection reset)")

type pending struct {
	id       uint64
	start    time.Time
	released chan Mode
}

// Fake is the controllable upstream.
type Fake struct {
	clock vclock.Clock

	mu      sync.Mutex
	mode    Mode
	nextID  uint64
	pending map[uint64]*pending
	log     []Attempt
}

// New creates a fake initially answering in initialMode.
func New(clock vclock.Clock, initialMode Mode) *Fake {
	if clock == nil {
		clock = vclock.SystemClock{}
	}
	return &Fake{
		clock:   clock,
		mode:    initialMode,
		pending: make(map[uint64]*pending),
	}
}

// SetMode changes the answer mode for subsequent calls.
func (f *Fake) SetMode(m Mode) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.mode = m
}

// Mode returns the current mode.
func (f *Fake) Mode() Mode {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.mode
}

// Pending returns the IDs of currently hanging calls, in arrival order.
func (f *Fake) Pending() []uint64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	ids := make([]uint64, 0, len(f.pending))
	for _, p := range f.pendingByIDLocked() {
		ids = append(ids, p.id)
	}
	return ids
}

// Release answers one hung call (the oldest by default, or a specific ID when
// given) as the given mode. It reports whether a hung call was found.
func (f *Fake) Release(as Mode, id ...uint64) bool {
	f.mu.Lock()
	var target *pending
	if len(id) > 0 {
		target = f.pending[id[0]]
	} else if ordered := f.pendingByIDLocked(); len(ordered) > 0 {
		target = ordered[0]
	}
	f.mu.Unlock()
	if target == nil {
		return false
	}
	target.released <- as
	return true
}

// ReleaseAll answers every hung call as the given mode and returns how many
// were released.
func (f *Fake) ReleaseAll(as Mode) int {
	f.mu.Lock()
	targets := f.pendingByIDLocked()
	f.mu.Unlock()
	for _, p := range targets {
		p.released <- as
	}
	return len(targets)
}

// Attempts returns a copy of the call log.
func (f *Fake) Attempts() []Attempt {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]Attempt, len(f.log))
	copy(out, f.log)
	return out
}

func (f *Fake) pendingByIDLocked() []*pending {
	out := make([]*pending, 0, len(f.pending))
	for _, p := range f.pending {
		out = append(out, p)
	}
	// Ordering is fixed by ID; map iteration is randomized.
	sortByID(out)
	return out
}

func sortByID(ps []*pending) {
	for i := 1; i < len(ps); i++ {
		for j := i; j > 0 && ps[j-1].id > ps[j].id; j-- {
			ps[j-1], ps[j] = ps[j], ps[j-1]
		}
	}
}

// Call performs one in-process call (used by the deterministic scenarios).
// In ModeHang it blocks until released even if ctx is canceled, so a failure
// can be delivered after the caller's deadline; callers that abandon the call
// simply discard the returned result.
func (f *Fake) Call(ctx context.Context) (statusCode int, err error) {
	f.mu.Lock()
	f.nextID++
	id := f.nextID
	mode := f.mode
	start := f.clock.Now()

	if mode != ModeHang {
		f.appendAttemptLocked(Attempt{ID: id, Start: start, End: start, Mode: mode, StatusCode: statusFor(mode)})
		f.mu.Unlock()
		return answerImmediate(mode)
	}

	p := &pending{id: id, start: start, released: make(chan Mode, 1)}
	f.pending[id] = p
	f.appendAttemptLocked(Attempt{ID: id, Start: start, Mode: ModeHang})
	f.mu.Unlock()

	var released bool
	select {
	case as := <-p.released:
		released = true
		f.finishPending(id, as, start)
		return answerImmediate(as)
	case <-ctx.Done():
		// The caller gave up, but the "server" is still working: wait
		// for the eventual release so the attempt log stays truthful.
		go func() {
			as := <-p.released
			f.finishPending(id, as, start)
		}()
		_ = released
		return 0, ctx.Err()
	}
}

func (f *Fake) finishPending(id uint64, as Mode, start time.Time) {
	f.mu.Lock()
	delete(f.pending, id)
	for i := range f.log {
		if f.log[i].ID == id {
			f.log[i].End = f.clock.Now()
			f.log[i].ReleasedAs = as
			f.log[i].StatusCode = statusFor(as)
			break
		}
	}
	f.mu.Unlock()
}

func (f *Fake) appendAttemptLocked(a Attempt) {
	f.log = append(f.log, a)
}

func answerImmediate(m Mode) (int, error) {
	switch m {
	case ModeOK:
		return http.StatusOK, nil
	case ModeFail:
		return http.StatusInternalServerError, nil
	case ModeError:
		return 0, ErrInjected
	default:
		return 0, fmt.Errorf("fakeupstream: unsupported released mode %q", m)
	}
}

func statusFor(m Mode) int {
	switch m {
	case ModeOK:
		return http.StatusOK
	case ModeFail:
		return http.StatusInternalServerError
	default:
		return 0
	}
}

// ServeHTTP exposes the fake over HTTP for the local demo service. The handler
// honors the same modes; hung requests ignore client cancellation and only
// finish on Release/ReleaseAll.
func (f *Fake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.nextID++
	id := f.nextID
	mode := f.mode
	start := f.clock.Now()

	if mode != ModeHang {
		f.appendAttemptLocked(Attempt{ID: id, Start: start, End: start, Mode: mode, StatusCode: statusFor(mode)})
		f.mu.Unlock()
		writeAnswer(w, mode)
		return
	}

	p := &pending{id: id, start: start, released: make(chan Mode, 1)}
	f.pending[id] = p
	f.appendAttemptLocked(Attempt{ID: id, Start: start, Mode: ModeHang})
	f.mu.Unlock()

	// Only an explicit release completes the response; client cancellation
	// does not, mirroring a server that keeps processing an abandoned call.
	as := <-p.released
	f.mu.Lock()
	delete(f.pending, id)
	for i := range f.log {
		if f.log[i].ID == id {
			f.log[i].End = f.clock.Now()
			f.log[i].ReleasedAs = as
			f.log[i].StatusCode = statusFor(as)
			break
		}
	}
	f.mu.Unlock()
	writeAnswer(w, as)
}

func writeAnswer(w http.ResponseWriter, m Mode) {
	switch m {
	case ModeOK:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true,"source":"fake-upstream"}`))
	case ModeFail:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"ok":false,"error":"injected failure"}`))
	case ModeError:
		// Best-effort transport failure: abort the body-less response.
		if hj, ok := w.(http.Hijacker); ok {
			conn, _, err := hj.Hijack()
			if err == nil {
				_ = conn.Close()
				return
			}
		}
		http.Error(w, `{"ok":false,"error":"injected transport error"}`, http.StatusBadGateway)
	default:
		http.Error(w, `{"ok":false,"error":"unknown mode"}`, http.StatusInternalServerError)
	}
}
