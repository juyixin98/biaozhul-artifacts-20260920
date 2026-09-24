package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"sseserver/internal/broker"
	"sseserver/internal/sse"
	"sseserver/internal/store"
)

type harness struct {
	*httptest.Server
	st     *store.Store
	broker *broker.Broker
}

func newHarness(t *testing.T, maxEvents, buffer int, heartbeat time.Duration) *harness {
	t.Helper()
	st, err := store.Open(store.Options{
		Dir:               filepath.Join(t.TempDir(), "data"),
		MaxEvents:         maxEvents,
		CompactEventDelta: 1,
		Sync:              true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })

	logger := log.New(io.Discard, "", 0)
	b := broker.New(st, broker.Config{BufferSize: buffer}, logger)
	srv := New(b, st, Config{Heartbeat: heartbeat, MaxDataLen: 4096}, logger)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return &harness{Server: ts, st: st, broker: b}
}

func (h *harness) publish(t *testing.T, data string) uint64 {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"data": data})
	resp, err := http.Post(h.URL+"/events", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("publish status=%d body=%s", resp.StatusCode, b)
	}
	var out struct {
		ID uint64 `json:"id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.ID
}

// openStream connects with an optional Last-Event-ID and returns the body.
func (h *harness) openStream(t *testing.T, lastID uint64) (*http.Response, io.ReadCloser) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, h.URL+"/events", nil)
	if err != nil {
		t.Fatal(err)
	}
	if lastID > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatUint(lastID, 10))
	}
	return h.doStream(t, req)
}

// openStreamReplayFrom0 attaches an explicit "Last-Event-ID: 0", requesting
// replay of the whole retained log (distinct from a headerless live-only
// subscription).
func (h *harness) openStreamReplayFrom0(t *testing.T) (*http.Response, io.ReadCloser) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, h.URL+"/events", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Last-Event-ID", "0")
	return h.doStream(t, req)
}

func (h *harness) doStream(t *testing.T, req *http.Request) (*http.Response, io.ReadCloser) {
	t.Helper()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("stream status=%d body=%s", resp.StatusCode, body)
	}
	return resp, resp.Body
}

// readEvent reads one dispatched SSE event with a deadline.
func readEvent(t *testing.T, dec *sse.Decoder) sse.Event {
	t.Helper()
	type res struct {
		ev  sse.Event
		err error
	}
	ch := make(chan res, 1)
	go func() {
		ev, err := dec.Next()
		ch <- res{ev, err}
	}()
	select {
	case r := <-ch:
		if r.err != nil {
			t.Fatalf("read event: %v", r.err)
		}
		return r.ev
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for event")
	}
	return sse.Event{}
}

// TestAcceptance_ReconnectAtReplayLiveSeam is the headline acceptance case:
// a client disconnects around the replay/live seam, reconnects repeatedly
// (once with a stale cursor that forces duplicates), and verifies the
// ID-deduped view is contiguous with no gaps.
func TestAcceptance_ReconnectAtReplayLiveSeam(t *testing.T) {
	h := newHarness(t, 0, 256, time.Hour)

	// Backlog exists before any connection.
	for i := 0; i < 20; i++ {
		h.publish(t, fmt.Sprintf("backlog-%d", i))
	}

	seen := map[uint64]bool{}
	dups := 0
	note := func(id uint64) {
		if seen[id] {
			dups++
		}
		seen[id] = true
	}

	// Conn A: fresh client replays the backlog; process 1..15 then drop the
	// connection (16..20 may already be in the socket buffer and are lost).
	var lastID uint64
	resp, body := h.openStreamReplayFrom0(t)
	dec := sse.NewDecoder(body)
	for lastID < 15 {
		ev := readEvent(t, dec)
		lastID = mustID(t, ev)
		note(lastID)
	}
	resp.Body.Close()

	// Offline window: 16..30 are appended while no client is connected.
	for i := 0; i < 15; i++ {
		h.publish(t, "offline")
	}

	// Conn B resumes at 15. The server replays 16..30 while a concurrent
	// publisher appends 31..55 — this is the replay/live seam stress.
	resp, body = h.openStream(t, lastID)
	dec = sse.NewDecoder(body)
	pubDone := make(chan struct{})
	go func() {
		for i := 0; i < 25; i++ {
			h.publish(t, "live")
			time.Sleep(2 * time.Millisecond)
		}
		close(pubDone)
	}()
	// Read through id 42 then disconnect mid-stream.
	for id := uint64(0); id < 42; {
		ev := readEvent(t, dec)
		id = mustID(t, ev)
		note(id)
		if id > lastID {
			lastID = id
		}
	}
	resp.Body.Close()
	<-pubDone

	// Conn C deliberately reconnects with a STALE cursor (40 while 41..42
	// were already processed): 41,42 must be redelivered and deduped; then
	// the replay continues seamlessly to 60 with no gap.
	resp, body = h.openStream(t, 40)
	dec = sse.NewDecoder(body)
	for lastID < 60 {
		ev := readEvent(t, dec)
		id := mustID(t, ev)
		note(id)
		if id > lastID {
			lastID = id
		}
	}
	resp.Body.Close()

	// Two redundant reconnects at the up-to-date cursor must deliver nothing.
	// Use a dedicated client per probe so an idle streaming connection never
	// pins the shared transport's connection pool.
	for i := 0; i < 2; i++ {
		probe := &http.Client{Timeout: 0}
		req, _ := http.NewRequest(http.MethodGet, h.URL+"/events", nil)
		req.Header.Set("Last-Event-ID", strconv.FormatUint(lastID, 10))
		r, err := probe.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		d := sse.NewDecoder(r.Body)
		ch := make(chan error, 1)
		go func() {
			_, gerr := d.Next()
			ch <- gerr
		}()
		select {
		case gerr := <-ch:
			if gerr == nil {
				t.Fatal("up-to-date resume unexpectedly delivered an event")
			}
		case <-time.After(200 * time.Millisecond): // no event: correct
		}
		r.Body.Close()
		probe.CloseIdleConnections()
	}

	var missing []uint64
	for id := uint64(1); id <= 60; id++ {
		if !seen[id] {
			missing = append(missing, id)
		}
	}
	if len(missing) > 0 {
		t.Fatalf("GAP after dedupe: missing=%v, duplicates collapsed=%d", missing, dups)
	}
	if dups < 2 {
		t.Fatalf("expected the stale-cursor reconnect to produce >=2 duplicates, got %d", dups)
	}
	t.Logf("seam acceptance OK: 1..60 contiguous, duplicates collapsed=%d", dups)
}

// TestAcceptance_RetentionBoundaryReset verifies that an over-old cursor gets
// an explicit reset carrying the retained window and the HTTP body closes.
func TestAcceptance_RetentionBoundaryReset(t *testing.T) {
	h := newHarness(t, 5, 64, time.Hour)
	for i := 0; i < 20; i++ {
		h.publish(t, "x")
	}
	bounds := h.st.Bounds() // oldest=16, last=20

	_, body := h.openStream(t, 10) // 10+1 < 16 -> expired
	defer body.Close()
	dec := sse.NewDecoder(body)
	ev := readEvent(t, dec)
	if ev.Type != "reset" {
		t.Fatalf("expected reset event, got type=%q", ev.Type)
	}
	if strings.Contains(ev.ID, "0") || ev.ID != "" {
		t.Fatalf("reset frame must not set id, got id=%q", ev.ID)
	}
	var payload struct {
		Reason string `json:"reason"`
		Oldest uint64 `json:"oldest"`
		Last   uint64 `json:"last"`
	}
	if err := json.Unmarshal([]byte(ev.Data), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Reason != "expired" || payload.Oldest != bounds.Oldest || payload.Last != bounds.Last {
		t.Fatalf("reset payload = %+v bounds=%+v", payload, bounds)
	}

	// The server closes the connection after the reset frame.
	ch := make(chan error, 1)
	go func() {
		_, err := dec.Next()
		ch <- err
	}()
	select {
	case err := <-ch:
		if err == nil {
			t.Fatal("expected EOF after reset")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("connection not closed after reset")
	}

	// Client follows instructions: reconnect with oldest-1, gets the window.
	_, body2 := h.openStream(t, bounds.Oldest-1)
	defer body2.Close()
	dec2 := sse.NewDecoder(body2)
	for want := bounds.Oldest; want <= bounds.Last; want++ {
		if id := mustID(t, readEvent(t, dec2)); id != want {
			t.Fatalf("post-reset replay expected %d got %d", want, id)
		}
	}
}

// TestAcceptance_HeartbeatsIgnored verifies heartbeat comment lines do not
// dispatch events and the stream stays alive across an idle period.
func TestAcceptance_HeartbeatsIgnored(t *testing.T) {
	h := newHarness(t, 0, 8, 60*time.Millisecond)
	id0 := h.publish(t, "before-idle")
	_, body := h.openStream(t, 0)
	defer body.Close()

	// Live-only connection: wait through ~4 heartbeat intervals, then publish.
	time.Sleep(300 * time.Millisecond)
	id1 := h.publish(t, "after-idle")

	dec := sse.NewDecoder(body)
	ev := readEvent(t, dec)
	gotID := mustID(t, ev)
	// Heartbeats must not have produced events; the next real event is id1.
	if gotID != id1 {
		t.Fatalf("expected id %d got %d (heartbeat dispatched?)", id1, gotID)
	}
	if id1 != id0+1 {
		t.Fatalf("id monotonicity broken: %d after %d", id1, id0)
	}
}

// TestAcceptance_SlowConsumerDisconnected verifies the server force-closes a
// stalled subscriber with an error frame instead of blocking publishers.
func TestAcceptance_SlowConsumerDisconnected(t *testing.T) {
	// Bounded-buffer enforcement is covered rigorously at the broker layer
	// (TestSlowConsumerLiveDropped / TestSlowConsumerDuringReplayDropped);
	// reproducing a true network stall over loopback is unreliable because
	// the kernel/transport buffers absorb small floods. This test verifies
	// the HTTP contract once the broker marks a subscriber dropped: the
	// handler writes an id-less slow_consumer frame and returns.
	st, err := store.Open(store.Options{Dir: filepath.Join(t.TempDir(), "data"), Sync: true})
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	discard := log.New(io.Discard, "", 0)
	b2 := broker.New(st, broker.Config{BufferSize: 2}, discard)
	srv2 := New(b2, st, Config{Heartbeat: time.Hour}, discard)

	req := httptest.NewRequest(http.MethodGet, "/events", nil)
	rec := httptest.NewRecorder() // implements http.Flusher
	served := make(chan struct{})
	go func() {
		srv2.Handler().ServeHTTP(rec, req)
		close(served)
	}()

	deadline := time.Now().Add(2 * time.Second)
	for b2.SubscriberCount() != 1 && time.Now().Before(deadline) {
		time.Sleep(2 * time.Millisecond)
	}
	if b2.SubscriberCount() != 1 {
		t.Fatal("subscriber never registered")
	}

	if n := b2.DropAllForTest(); n != 1 {
		t.Fatalf("dropped %d subscribers, want 1", n)
	}

	select {
	case <-served:
	case <-time.After(2 * time.Second):
		t.Fatal("handler did not return after the subscriber was dropped")
	}

	body := rec.Body.String()
	if !strings.Contains(body, "event: error") {
		t.Fatalf("missing error frame; body=%q", body)
	}
	if !strings.Contains(body, "slow_consumer") {
		t.Fatalf("error frame payload wrong; body=%q", body)
	}
	if strings.Contains(body, "\nid:") || strings.HasPrefix(body, "id:") {
		t.Fatalf("error frame must not set an id; body=%q", body)
	}
}

// TestMultilineDataEncoding verifies spec-correct multi-line data fields.
func TestMultilineDataEncoding(t *testing.T) {
	h := newHarness(t, 0, 8, time.Hour)
	id := h.publish(t, "line1\nline2\nline3") // id == 1
	_, body := h.openStreamReplayFrom0(t)
	defer body.Close()
	dec := sse.NewDecoder(body)
	ev := readEvent(t, dec)
	if ev.ID != strconv.FormatUint(id, 10) {
		t.Fatalf("multiline frame id=%q want %d", ev.ID, id)
	}
	if ev.Data != "line1\nline2\nline3" {
		t.Fatalf("multiline data reassembled wrong: %q", ev.Data)
	}

	// CRLF input is normalized to LF; resume just before id2 to replay it.
	id2 := h.publish(t, "a\r\nb\rc")
	_, body2 := h.openStream(t, id2-1)
	defer body2.Close()
	ev2 := readEvent(t, sse.NewDecoder(body2))
	if ev2.Data != "a\nb\nc" {
		t.Fatalf("CRLF normalization wrong: %q", ev2.Data)
	}
}

func TestInvalidLastEventID(t *testing.T) {
	h := newHarness(t, 0, 8, time.Hour)
	req, _ := http.NewRequest(http.MethodGet, h.URL+"/events", nil)
	req.Header.Set("Last-Event-ID", "not-a-number")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}
}

func TestStatsEndpoint(t *testing.T) {
	h := newHarness(t, 0, 8, time.Hour)
	h.publish(t, "a")
	h.publish(t, "b")
	resp, err := http.Get(h.URL + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var stats map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		t.Fatal(err)
	}
	if stats["retained_events"].(float64) != 2 {
		t.Fatalf("stats=%v", stats)
	}
	if stats["last_id"].(float64) != 2 {
		t.Fatalf("stats last_id=%v", stats["last_id"])
	}
}

func TestHealthz(t *testing.T) {
	h := newHarness(t, 0, 8, time.Hour)
	resp, err := http.Get(h.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func mustID(t *testing.T, ev sse.Event) uint64 {
	t.Helper()
	id, err := strconv.ParseUint(ev.ID, 10, 64)
	if err != nil {
		t.Fatalf("non-numeric event id %q (type=%s data=%q)", ev.ID, ev.Type, ev.Data)
	}
	return id
}
