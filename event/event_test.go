package event

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

type fixedClock struct{ t time.Time }

func (f fixedClock) Now() time.Time { return f.t }

func TestRecorderOrderingAndContent(t *testing.T) {
	sink := NewMemorySink(100)
	rec := NewRecorder(fixedClock{t: time.UnixMilli(42)}, sink)
	rec.Emit(Submitted, "j1", "admitted", map[string]any{"demand": 2})
	rec.Emit(Started, "j1", "", nil)

	got := sink.Events()
	if len(got) != 2 {
		t.Fatalf("events=%d want 2", len(got))
	}
	if got[0].Seq != 1 || got[1].Seq != 2 {
		t.Fatalf("seqs %d,%d want 1,2", got[0].Seq, got[1].Seq)
	}
	if got[0].TimeMs != 42 || got[1].TimeMs != 42 {
		t.Fatalf("time stamps %d,%d want 42", got[0].TimeMs, got[1].TimeMs)
	}
	if got[0].Type != Submitted || got[0].JobID != "j1" || got[0].Reason != "admitted" {
		t.Fatalf("first event wrong: %+v", got[0])
	}
	if got[0].Detail["demand"] != 2 {
		t.Fatalf("detail=%v", got[0].Detail)
	}
}

func TestMemorySinkCapacity(t *testing.T) {
	sink := NewMemorySink(2)
	for i := 0; i < 5; i++ {
		sink.Write(Event{Seq: int64(i + 1)})
	}
	got := sink.Events()
	if len(got) != 2 {
		t.Fatalf("len=%d want 2", len(got))
	}
	if got[0].Seq != 4 || got[1].Seq != 5 {
		t.Fatalf("oldest should be dropped, got seqs %d,%d", got[0].Seq, got[1].Seq)
	}
	if sink.Dropped() != 3 {
		t.Fatalf("dropped=%d want 3", sink.Dropped())
	}
}

func TestJSONLSink(t *testing.T) {
	var buf bytes.Buffer
	sink := NewJSONLSink(&buf)
	rec := NewRecorder(fixedClock{}, sink)
	rec.Emit(Started, "j", "", map[string]any{"k": "v"})
	line := strings.TrimSpace(buf.String())
	var m map[string]any
	if err := json.Unmarshal([]byte(line), &m); err != nil {
		t.Fatalf("invalid JSON line %q: %v", line, err)
	}
	if m["type"] != string(Started) || m["job_id"] != "j" {
		t.Fatalf("decoded=%v", m)
	}
}

func TestMulti(t *testing.T) {
	a, b := NewMemorySink(10), NewMemorySink(10)
	Multi(a, b).Write(Event{Seq: 1})
	if len(a.Events()) != 1 || len(b.Events()) != 1 {
		t.Fatal("multi must fan out to both sinks")
	}
}
