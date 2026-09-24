package server

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"sse-resume/internal/broker"
)

// ---- test SSE parser -------------------------------------------------------

type sseFrame struct {
	id      string
	event   string
	data    string
	comment bool
}

type sseStream struct {
	resp *http.Response
	br   *bufio.Reader
	body bytes.Buffer // raw bytes captured (for assertions on framing)
}

func openStream(t testing.TB, h http.Handler, cursor string) *sseStream {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return openStreamURL(t, srv.URL+"/v1/events/stream", cursor)
}

func openStreamURL(t testing.TB, url, cursor string) *sseStream {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	if cursor != "" {
		req.Header.Set("Last-Event-ID", cursor)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("open stream: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status = %d body=%s", resp.StatusCode, body)
	}
	return &sseStream{resp: resp, br: bufio.NewReader(resp.Body)}
}

func (s *sseStream) next(t *testing.T, timeout time.Duration) sseFrame {
	t.Helper()
	type res struct {
		f   sseFrame
		err error
	}
	ch := make(chan res, 1)
	go func() {
		f, err := s.readFrame()
		ch <- res{f, err}
	}()
	select {
	case r := <-ch:
		if r.err != nil {
			t.Fatalf("read frame: %v", r.err)
		}
		return r.f
	case <-time.After(timeout):
		t.Fatalf("timed out waiting for frame after %s; raw=%q", timeout, s.body.String())
		return sseFrame{}
	}
}

func (s *sseStream) close() {
	s.resp.Body.Close()
}

func (s *sseStream) readFrame() (sseFrame, error) {
	var lines []string
	for {
		line, err := s.br.ReadString('\n')
		s.body.WriteString(line)
		if err != nil {
			return sseFrame{}, err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		lines = append(lines, line)
	}
	f := sseFrame{}
	var dataLines []string
	commentOnly := true
	for _, line := range lines {
		if strings.HasPrefix(line, ":") {
			continue
		}
		commentOnly = false
		field, value := line, ""
		if i := strings.IndexByte(line, ':'); i >= 0 {
			field = line[:i]
			value = strings.TrimPrefix(line[i+1:], " ")
		}
		switch field {
		case "id":
			f.id = value
		case "event":
			f.event = value
		case "data":
			dataLines = append(dataLines, value)
		}
	}
	f.data = strings.Join(dataLines, "\n")
	f.comment = commentOnly && f.event == "" && f.id == "" && len(dataLines) == 0
	return f, nil
}

func mustEventFrame(t *testing.T, f sseFrame, wantID string) {
	t.Helper()
	if f.id != wantID || f.event != "msg" {
		t.Fatalf("frame = id=%q event=%q data=%q, want id=%s event=msg", f.id, f.event, f.data, wantID)
	}
}

// ---- test harness ----------------------------------------------------------

type harness struct {
	srv *httptest.Server
	br  *broker.Broker
}

func newHarness(t *testing.T, maxEvents, queue int, heartbeat time.Duration) *harness {
	t.Helper()
	return newHarnessGate(t, maxEvents, queue, heartbeat, nil)
}

func newHarnessGate(t *testing.T, maxEvents, queue int, heartbeat time.Duration, gate *flushGate) *harness {
	t.Helper()
	br, err := broker.Open(broker.Config{
		Dir: t.TempDir(), MaxEvents: maxEvents, QueueSize: queue, NoSync: true,
	})
	if err != nil {
		t.Fatalf("broker open: %v", err)
	}
	t.Cleanup(func() { _ = br.Close() })

	srv := server0(br, heartbeat, gate)
	t.Cleanup(srv.Close)
	return &harness{srv: srv, br: br}
}

func server0(br *broker.Broker, heartbeat time.Duration, gate *flushGate) *httptest.Server {
	s := New(br, WithHeartbeat(heartbeat), WithWriteTimeout(time.Second))
	s.gate = gate
	srv := httptest.NewServer(s.Handler())
	return srv
}

func (hc *harness) publish(t *testing.T, data string) int64 {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"event": "msg", "data": data})
	resp, err := http.Post(hc.srv.URL+"/v1/events", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("publish status=%d body=%s", resp.StatusCode, b)
	}
	var pr struct {
		ID int64 `json:"id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&pr); err != nil {
		t.Fatalf("decode publish resp: %v", err)
	}
	return pr.ID
}

func (hc *harness) stream(t testing.TB, cursor string) *sseStream {
	t.Helper()
	return openStreamURL(t, hc.srv.URL+"/v1/events/stream", cursor)
}

// frameOrError waits for a frame but tolerates EOF (server-side close),
// returning err instead of failing the test.
func frameOrError(t *testing.T, s *sseStream, timeout time.Duration) (sseFrame, error) {
	t.Helper()
	ch := make(chan error, 1)
	var f sseFrame
	go func() {
		var err error
		f, err = s.readFrame()
		ch <- err
	}()
	select {
	case err := <-ch:
		return f, err
	case <-time.After(timeout):
		return f, fmt.Errorf("timeout; raw=%q", s.body.String())
	}
}

// ---- tests -----------------------------------------------------------------

func TestPublishValidation(t *testing.T) {
	hc := newHarness(t, 0, 4, time.Hour)

	post := func(raw string) int {
		resp, err := http.Post(hc.srv.URL+"/v1/events", "application/json", strings.NewReader(raw))
		if err != nil {
			t.Fatalf("post: %v", err)
		}
		defer resp.Body.Close()
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode
	}
	if code := post(`{"event":"msg","data":""}`); code != http.StatusBadRequest {
		t.Fatalf("empty data status=%d, want 400", code)
	}
	if code := post(`{"event":"bad\nname","data":"x"}`); code != http.StatusBadRequest {
		t.Fatalf("bad event name status=%d, want 400", code)
	}
	if code := post(`{not json`); code != http.StatusBadRequest {
		t.Fatalf("malformed json status=%d, want 400", code)
	}
}

func TestResumeFullBacklogThenLive(t *testing.T) {
	hc := newHarness(t, 0, 8, time.Hour)
	id1 := hc.publish(t, "one")
	id2 := hc.publish(t, "two")
	if id1 != 1 || id2 != 2 {
		t.Fatalf("ids = %d,%d", id1, id2)
	}

	s := hc.stream(t, "")
	defer s.close()
	f := s.next(t, time.Second)
	if f.event != "ready" {
		t.Fatalf("first frame event=%q, want ready", f.event)
	}
	mustEventFrame(t, s.next(t, time.Second), "1")
	mustEventFrame(t, s.next(t, time.Second), "2")

	id3 := hc.publish(t, "three")
	mustEventFrame(t, s.next(t, time.Second), fmt.Sprint(id3))
}

func TestLastEventIDHeaderResume(t *testing.T) {
	hc := newHarness(t, 0, 8, time.Hour)
	for _, d := range []string{"a", "b", "c", "d"} {
		hc.publish(t, d)
	}
	s := hc.stream(t, "2") // resume strictly after 2
	defer s.close()
	s.next(t, time.Second) // ready
	mustEventFrame(t, s.next(t, time.Second), "3")
	mustEventFrame(t, s.next(t, time.Second), "4")
	hc.publish(t, "e")
	mustEventFrame(t, s.next(t, time.Second), "5")
}

// Acceptance scenario #1: disconnect exactly at the replay/live seam while
// publishing continues; reconnect with Last-Event-ID and verify every id 1..N
// is delivered exactly once after client-side dedup (at least the seam dup).
func TestBoundaryDisconnectAndReconnect(t *testing.T) {
	hc := newHarness(t, 0, 32, time.Hour)

	// Events 1..10 exist before the client connects.
	for i := 0; i < 10; i++ {
		hc.publish(t, fmt.Sprintf("e%d", i+1))
	}

	seen := map[int64]int{}
	var dups int
	record := func(f sseFrame) {
		var id int64
		fmt.Sscanf(f.id, "%d", &id)
		seen[id]++
		if seen[id] > 1 {
			dups++
		}
	}

	// First connection: consume replay 1..K then disconnect mid-stream.
	s := hc.stream(t, "")
	s.next(t, time.Second) // ready
	const k = 5
	for i := 0; i < k; i++ {
		record(s.next(t, time.Second))
	}
	s.close()

	// New events arrive while the client is offline (including id 11 after close).
	hc.publish(t, "e11")

	// Reconnect from last seen id=5: server replays 6..10+11, exactly-once
	// across the union because 5 was already consumed (not resent).
	s2 := hc.stream(t, fmt.Sprint(k))
	s2.next(t, time.Second) // ready
	for i := 0; i < 6; i++ {
		f := s2.next(t, 2*time.Second)
		if f.id == "" {
			t.Fatalf("unexpected control frame event=%q data=%q", f.event, f.data)
		}
		record(f)
	}

	// Now reconnect WITHOUT consuming new live frame, immediately close &
	// reopen from the SAME cursor to force a seam duplicate: ids 6..11 get
	// replayed again and deduped.
	s2.close()
	s3 := hc.stream(t, fmt.Sprint(k))
	s3.next(t, time.Second) // ready
	for i := 0; i < 6; i++ {
		record(s3.next(t, 2*time.Second))
	}
	defer s3.close()

	if dups == 0 {
		t.Fatal("expected at least one seam duplicate; got none (resume did not redeliver)")
	}
	for id := int64(1); id <= 11; id++ {
		if n := seen[id]; n == 0 {
			t.Fatalf("missing id %d after reconnects (no gap allowed)", id)
		}
	}
	if len(seen) != 11 {
		t.Fatalf("unique ids = %d, want 11", len(seen))
	}
	t.Logf("OK: 11 unique ids, %d duplicate redeliveries discarded by dedup", dups)
}

// Acceptance scenario #2: repeated reconnects while a publisher keeps
// firing; union must be contiguous.
func TestRepeatedReconnectsUnderLoad(t *testing.T) {
	hc := newHarness(t, 0, 64, time.Hour)
	const total = 60
	pubCh := make(chan struct{})
	go func() {
		for i := 0; i < total; i++ {
			hc.publish(t, fmt.Sprintf("bulk%d", i+1))
			time.Sleep(5 * time.Millisecond)
		}
		close(pubCh)
	}()

	seen := map[int64]struct{}{}
	cursor := ""
	for round := 0; ; round++ {
		s := hc.stream(t, cursor)
		gotReady := false
	consume:
		for {
			f, err := frameOrError(t, s, 400*time.Millisecond)
			if err != nil {
				break consume
			}
			if f.comment {
				continue
			}
			if f.event == "ready" {
				gotReady = true
				continue
			}
			if f.id == "" {
				t.Fatalf("unexpected control frame: %q", f.event)
			}
			var id int64
			fmt.Sscanf(f.id, "%d", &id)
			seen[id] = struct{}{}
			cursor = f.id
			if id == total {
				break consume
			}
		}
		s.close()
		if !gotReady {
			t.Fatalf("round %d never received ready frame; raw=%q", round, s.body.String())
		}
		if _, done := seen[total]; done {
			break
		}
		if round > 50 {
			t.Fatalf("too many reconnect rounds; have %d/%d", len(seen), total)
		}
	}
	<-pubCh
	for id := int64(1); id <= total; id++ {
		if _, ok := seen[id]; !ok {
			t.Fatalf("missing id %d after repeated reconnects", id)
		}
	}
	t.Logf("OK: received all %d ids with repeated disconnect/reconnect cycles", total)
}

// Acceptance scenario #3: retention boundary. After the window slides past a
// cursor, the stream must deliver a reset frame and the replay endpoint 409.
func TestRetentionReset(t *testing.T) {
	hc := newHarness(t, 5, 8, time.Hour)
	for i := 0; i < 5; i++ {
		hc.publish(t, "old")
	}
	// Force the window to slide: ids 1..4 expire, 5..9 retained.
	for i := 0; i < 4; i++ {
		hc.publish(t, "new")
	}
	if st := hc.br.Stats(); st.OldestID != 5 || st.Retained != 5 {
		t.Fatalf("window = %+v", st)
	}

	s := hc.stream(t, "2") // expired cursor
	f, err := frameOrError(t, s, time.Second)
	if err != nil {
		t.Fatalf("read reset: %v", err)
	}
	if f.event != "reset" || f.id != "" {
		t.Fatalf("frame = %+v, want reset with no id line", f)
	}
	if !strings.Contains(f.data, "cursor_expired") {
		t.Fatalf("reset data = %q, want cursor_expired", f.data)
	}
	// Server closes right after reset.
	if _, err := frameOrError(t, s, time.Second); err == nil {
		t.Fatal("expected stream close after reset frame")
	}
	s.close()

	// Ahead-of-head cursor also resets.
	s2 := hc.stream(t, "999")
	f2, _ := frameOrError(t, s2, time.Second)
	if f2.event != "reset" || !strings.Contains(f2.data, "cursor_ahead") {
		t.Fatalf("frame = %+v, want reset cursor_ahead", f2)
	}
	s2.close()

	// Non-SSE replay query reports 409 with resync instructions.
	resp, err := http.Get(hc.srv.URL + "/v1/events?after=2")
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("replay expired status=%d, want 409", resp.StatusCode)
	}
	var payload map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&payload)
	if payload["reason"] != "cursor_expired" || payload["resolution"] == nil {
		t.Fatalf("409 payload = %v", payload)
	}

	// Malformed cursor is a plain 400 before any SSE frame.
	req, _ := http.NewRequest(http.MethodGet, hc.srv.URL+"/v1/events/stream", nil)
	req.Header.Set("Last-Event-ID", "abc")
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("bad cursor: %v", err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad cursor status=%d, want 400", resp2.StatusCode)
	}
}

// Acceptance scenario #4: slow consumer whose server-side queue fills is
// evicted and receives a slow_consumer notice; after reconnect it can
// resume from retained history with no gap. A test-only flush gate makes
// the "peer stopped draining writes" condition deterministic.
func TestSlowConsumerEviction(t *testing.T) {
	gate := newFlushGate()
	hc := newHarnessGate(t, 0, 2, time.Hour, gate)

	s := hc.stream(t, "")
	// Wait until the ready frame's flush has happened, then freeze flushes.
	s.next(t, time.Second)
	gate.block()

	pubDone := make(chan struct{})
	go func() {
		defer close(pubDone)
		for i := 0; i < 16; i++ {
			hc.publish(t, fmt.Sprintf("payload-line-%d", i))
		}
	}()

	// Handler is now parked in Flush; the 2-slot subscriber queue fills and
	// the subscriber is evicted. Publishing itself is never blocked (fan-out
	// is non-blocking), so it completes quickly; we only wait for the drop.
	requireEventually(t, func() bool { return hc.br.Stats().Dropped >= 1 }, 3*time.Second)
	<-pubDone
	gate.resumeFlushes()

	// The handler was parked mid-flush when the queue overflowed. Buffered
	// events already handed to it (id 1..3) are still drained; after them
	// the closed channel yields the slow_consumer notice and EOF.
	var sawNotice bool
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		f, err := frameOrError(t, s, time.Second)
		if err != nil {
			break
		}
		if f.event == "slow_consumer" {
			sawNotice = true
		}
	}
	if !sawNotice {
		t.Fatalf("slow_consumer notice not sent; raw=%q", s.body.String())
	}
	s.close()

	// Reconnected reader catches up from id 0 with no gap (events retained).
	s2 := hc.stream(t, "0")
	s2.next(t, time.Second) // ready
	got := map[int64]struct{}{}
	for i := 0; i < 16; i++ {
		f := s2.next(t, 2*time.Second)
		var id int64
		fmt.Sscanf(f.id, "%d", &id)
		got[id] = struct{}{}
	}
	s2.close()
	for id := int64(1); id <= 16; id++ {
		if _, ok := got[id]; !ok {
			t.Fatalf("post-eviction resume missing id %d", id)
		}
	}
}

func requireEventually(t *testing.T, cond func() bool, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", within)
}

// Heartbeats are delivered on an idle stream.
func TestHeartbeat(t *testing.T) {
	hc := newHarness(t, 0, 4, 60*time.Millisecond)
	s := hc.stream(t, "")
	defer s.close()
	s.next(t, time.Second) // ready
	f := s.next(t, 2*time.Second)
	if !f.comment {
		t.Fatalf("expected comment heartbeat, got %+v", f)
	}
}

// Multi-line data round-trips through real HTTP.
func TestMultilineDataOverHTTP(t *testing.T) {
	hc := newHarness(t, 0, 4, time.Hour)
	payload := "first line\nsecond line\n\nfourth after blank"
	body, _ := json.Marshal(map[string]string{"event": "doc", "data": payload})
	resp, err := http.Post(hc.srv.URL+"/v1/events", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	resp.Body.Close()

	s := hc.stream(t, "")
	defer s.close()
	s.next(t, time.Second) // ready
	f := s.next(t, time.Second)
	if f.id != "1" || f.event != "doc" || f.data != payload {
		t.Fatalf("multiline mismatch:\n got=%q\nwant=%q (raw=%q)", f.data, payload, s.body.String())
	}
}

// Client going away mid-stream must not break publishing or other clients.
func TestClientDisconnectIsolated(t *testing.T) {
	hc := newHarness(t, 0, 8, time.Hour)
	s1 := hc.stream(t, "")
	s1.next(t, time.Second)
	s2 := hc.stream(t, "")
	s2.next(t, time.Second)
	s1.close()

	id := hc.publish(t, "alive")
	f := s2.next(t, time.Second)
	if f.id != fmt.Sprint(id) {
		t.Fatalf("surviving client got id=%q, want %d", f.id, id)
	}
	s2.close()
}

// Persistence across an actual server/broker restart.
func TestRestartResumesIDs(t *testing.T) {
	dir := t.TempDir()
	br1, err := broker.Open(broker.Config{Dir: dir, NoSync: true})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	srv := httptest.NewServer(New(br1, WithHeartbeat(time.Hour)).Handler())
	body, _ := json.Marshal(map[string]string{"data": "before restart"})
	resp, _ := http.Post(srv.URL+"/v1/events", "application/json", bytes.NewReader(body))
	resp.Body.Close()
	srv.Close()
	_ = br1.Close()

	br2, err := broker.Open(broker.Config{Dir: dir, NoSync: true})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer br2.Close()
	srv2 := httptest.NewServer(New(br2, WithHeartbeat(time.Hour)).Handler())
	defer srv2.Close()

	resp2, err := http.Post(srv2.URL+"/v1/events", "application/json",
		strings.NewReader(`{"event":"msg","data":"after restart"}`))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	var pr struct {
		ID int64 `json:"id"`
	}
	json.NewDecoder(resp2.Body).Decode(&pr)
	resp2.Body.Close()
	if pr.ID != 2 {
		t.Fatalf("id after restart = %d, want 2", pr.ID)
	}

	s := openStreamURL(t, srv2.URL+"/v1/events/stream", "1")
	defer s.close()
	s.next(t, time.Second)
	mustEventFrame(t, s.next(t, time.Second), "2")
}
