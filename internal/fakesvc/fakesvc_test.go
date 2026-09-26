package fakesvc

import (
	"context"
	"testing"
	"time"

	"graceful-shutdown/internal/clock"
)

func TestFakeDBQuerySuccessAndCancel(t *testing.T) {
	clk := clock.NewFake(time.Unix(0, 0))
	db := NewFakeDB(clk)
	go func() { clk.Advance(60 * time.Millisecond) }()
	res, err := db.Query(context.Background(), "x")
	if err != nil || res != "result(x)" {
		t.Fatalf("query = %q, %v", res, err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, e := db.SlowQuery(ctx, "slow", time.Hour)
		done <- e
	}()
	deadline := time.Now().Add(time.Second)
	for clk.Pending() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != context.Canceled {
			t.Fatalf("err = %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("SlowQuery did not react to cancellation")
	}
}

func TestFakeDBRejectsAfterClose(t *testing.T) {
	db := NewFakeDB(clock.Real{})
	if err := db.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Query(context.Background(), "x"); err == nil {
		t.Fatal("query after close must fail")
	}
}

func TestFakeQueuePublishAndClose(t *testing.T) {
	clk := clock.NewFake(time.Unix(0, 0))
	q := NewFakeQueue(clk)
	go func() { clk.Advance(20 * time.Millisecond) }()
	if err := q.Publish(context.Background(), "m"); err != nil {
		t.Fatalf("publish = %v", err)
	}
	if err := q.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := q.Publish(context.Background(), "m"); err == nil {
		t.Fatal("publish after close must fail")
	}
	if q.Name() != "fake-queue" {
		t.Errorf("name = %s", q.Name())
	}
}
