package fakedep_test

import (
	"context"
	"net/http"
	"testing"
	"time"

	"gracefulshutdown/internal/fakedep"
)

func TestLatencyAndFailureInjection(t *testing.T) {
	t.Parallel()
	s := fakedep.New()
	defer s.Close(context.Background())

	s.SetFault(fakedep.Fault{Fail: true})
	resp, err := http.Get(s.URL() + "/work")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("status=%d, want 500", resp.StatusCode)
	}
}

func TestHangBlocksUntilReleasedOrContextCancelled(t *testing.T) {
	t.Parallel()
	s := fakedep.New()
	defer s.Close(context.Background())
	s.SetFault(fakedep.Fault{Hang: true})

	done := make(chan int, 2)
	go func() {
		resp, err := http.Get(s.URL() + "/work")
		if err != nil {
			done <- -1
			return
		}
		resp.Body.Close()
		done <- resp.StatusCode
	}()

	time.Sleep(100 * time.Millisecond)
	if n := s.ActiveHangs(); n != 1 {
		t.Fatalf("active hangs=%d, want 1", n)
	}
	s.ReleaseHangs()
	select {
	case status := <-done:
		if status != http.StatusOK {
			t.Fatalf("after release status=%d, want 200", status)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("hung request did not return after release")
	}
}

func TestCloseUnavailableAndReleasesHangs(t *testing.T) {
	t.Parallel()
	s := fakedep.New()
	s.SetFault(fakedep.Fault{Hang: true})

	returned := make(chan struct{})
	go func() {
		resp, _ := http.Get(s.URL() + "/work")
		if resp != nil {
			resp.Body.Close()
		}
		close(returned)
	}()
	time.Sleep(100 * time.Millisecond)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := s.Close(ctx); err != nil {
		t.Fatalf("close: %v", err)
	}
	select {
	case <-returned:
	case <-time.After(2 * time.Second):
		t.Fatal("hung request still blocked after dependency close")
	}

	resp, err := http.Get(s.URL() + "/health")
	if err == nil {
		resp.Body.Close()
		t.Fatal("expected listener to be closed after Close")
	}
}
