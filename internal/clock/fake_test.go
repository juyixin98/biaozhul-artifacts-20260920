package clock

import (
	"testing"
	"time"
)

func TestFakeTimerFiresAfterAdvance(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	ch := f.After(5 * time.Second)

	select {
	case <-ch:
		t.Fatal("timer fired before Advance")
	default:
	}

	f.Advance(4 * time.Second)
	select {
	case <-ch:
		t.Fatal("timer fired before deadline")
	default:
	}

	f.Advance(1 * time.Second)
	select {
	case <-ch:
	default:
		t.Fatal("timer did not fire at deadline")
	}
}

func TestFakeTimerStop(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	tm := f.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("Stop on pending timer should return true")
	}
	f.Advance(2 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
}

func TestFakeSleep(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	done := make(chan struct{})
	go func() {
		f.Sleep(3 * time.Second)
		close(done)
	}()
	// Wait until the goroutine has registered its timer before advancing.
	deadline := time.Now().Add(time.Second)
	for f.Pending() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if f.Pending() == 0 {
		t.Fatal("sleeper never registered a timer")
	}
	f.Advance(2 * time.Second)
	select {
	case <-done:
		t.Fatal("Sleep returned early")
	default:
	}
	f.Advance(1 * time.Second)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Sleep did not return after advancing past duration")
	}
}
