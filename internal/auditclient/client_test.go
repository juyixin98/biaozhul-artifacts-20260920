package auditclient

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"idempotentsave/internal/fakeaudit"
)

func TestRecordSuccess(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer ts.Close()

	c := New(ts.URL)
	if err := c.Record(context.Background(), fakeaudit.Event{TxID: "t1"}); err != nil {
		t.Fatalf("record: %v", err)
	}
}

func TestRecordNon2xxIsError(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer ts.Close()

	c := New(ts.URL)
	if err := c.Record(context.Background(), fakeaudit.Event{}); err == nil {
		t.Fatal("503 must be returned as an error (caller cannot know whether it persisted)")
	}
}

func TestRecordServerDownIsError(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	ts.Close() // 立刻关闭，制造连接失败

	c := New(ts.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := c.Record(ctx, fakeaudit.Event{}); err == nil {
		t.Fatal("connection failure must be an error")
	}
}
