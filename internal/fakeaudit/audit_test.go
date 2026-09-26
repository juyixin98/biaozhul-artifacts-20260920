package fakeaudit

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func postEvent(t *testing.T, ts *httptest.Server, e Event) (*http.Response, []byte) {
	t.Helper()
	b, _ := json.Marshal(e)
	resp, err := http.Post(ts.URL+"/audit/events", "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("post event: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	return resp, body
}

func TestEventHappyPath(t *testing.T) {
	s := New()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	resp, body := postEvent(t, ts, Event{TxID: "t1", Account: "a", Amount: 10, IdemKey: "k", Attempt: 1})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("status=%d body=%s", resp.StatusCode, body)
	}
	if s.Calls() != 1 || s.EventCount() != 1 {
		t.Fatalf("calls=%d events=%d want 1/1", s.Calls(), s.EventCount())
	}
	ev := s.Events()
	if len(ev) != 1 || ev[0].TxID != "t1" {
		t.Fatalf("unexpected events: %+v", ev)
	}
}

func TestFailNextNDoesNotStore(t *testing.T) {
	s := New()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	s.FailNextN(1)

	resp, _ := postEvent(t, ts, Event{TxID: "t1"})
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status=%d want 503", resp.StatusCode)
	}
	if s.Calls() != 1 || s.EventCount() != 0 {
		t.Fatalf("plain failure must not store: calls=%d events=%d", s.Calls(), s.EventCount())
	}
	// 故障次数耗尽后恢复正常
	resp2, _ := postEvent(t, ts, Event{TxID: "t2"})
	if resp2.StatusCode != http.StatusCreated {
		t.Fatalf("status=%d want 201 after fault budget", resp2.StatusCode)
	}
}

func TestRecordThenFailNextNStoresButReturnsError(t *testing.T) {
	s := New()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	s.RecordThenFailNextN(1)

	resp, _ := postEvent(t, ts, Event{TxID: "t1"})
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status=%d want 503", resp.StatusCode)
	}
	if s.EventCount() != 1 {
		t.Fatalf("record-then-fail MUST persist the event: events=%d want 1", s.EventCount())
	}
}

func TestBadMethodAndBadBody(t *testing.T) {
	s := New()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/audit/events")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET status=%d want 405", resp.StatusCode)
	}

	resp2, err := http.Post(ts.URL+"/audit/events", "application/json", strings.NewReader("not-json"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json status=%d want 400", resp2.StatusCode)
	}
}

func TestFaultsEndpointAndInspectAndReset(t *testing.T) {
	s := New()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	// 通过 HTTP 控制面注入故障
	resp, err := http.Post(ts.URL+"/audit/faults", "application/json",
		strings.NewReader(`{"fail_next_n":2}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("faults status=%d", resp.StatusCode)
	}

	postEvent(t, ts, Event{TxID: "x"}) // 503，不记录
	inspect, _ := http.Get(ts.URL + "/audit/inspect")
	var view struct {
		Calls  int     `json:"calls"`
		Events []Event `json:"events"`
	}
	if err := json.NewDecoder(inspect.Body).Decode(&view); err != nil {
		t.Fatal(err)
	}
	inspect.Body.Close()
	if view.Calls != 1 || len(view.Events) != 0 {
		t.Fatalf("inspect after 1 failing call: %+v", view)
	}

	reset, _ := http.Post(ts.URL+"/audit/reset", "application/json", nil)
	reset.Body.Close()
	if s.Calls() != 0 || s.EventCount() != 0 {
		t.Fatalf("reset did not clear: calls=%d events=%d", s.Calls(), s.EventCount())
	}

	// 非法方法/正文
	r1, _ := http.Get(ts.URL + "/audit/faults")
	r1.Body.Close()
	if r1.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("faults GET status=%d want 405", r1.StatusCode)
	}
}
