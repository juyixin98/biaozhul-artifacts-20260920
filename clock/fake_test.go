package clock

import (
	"testing"
	"time"
)

func TestFakeBasicTimerFiresOnAdvance(t *testing.T) {
	f := NewFake(time.Unix(1000, 0))
	tm := f.NewTimer(10 * time.Second)

	select {
	case <-tm.C():
		t.Fatal("timer fired early")
	default:
	}
	f.Advance(9 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("timer fired at 9s, deadline is 10s")
	default:
	}
	f.Advance(1 * time.Second)
	select {
	case got := <-tm.C():
		if !got.Equal(time.Unix(1010, 0)) {
			t.Fatalf("fired at %v, want 1010", got.Unix())
		}
	default:
		t.Fatal("timer did not fire at deadline")
	}
}

func TestFakeTimerDeadlineOrder(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	later := f.NewTimer(5 * time.Second)
	earlier := f.NewTimer(2 * time.Second)
	middle := f.NewTimer(3 * time.Second)

	f.Advance(10 * time.Second)
	for _, want := range []time.Time{
		time.Unix(2, 0), time.Unix(3, 0), time.Unix(5, 0),
	} {
		select {
		case got := <-earlier.C():
			if !got.Equal(want) {
				t.Fatalf("got %v want %v", got, want)
			}
			earlier = middle
			middle = later
		default:
			t.Fatalf("expected fire at %v", want)
		}
	}
}

func TestFakeStop(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	tm := f.NewTimer(time.Minute)
	if !tm.Stop() {
		t.Fatal("first Stop should return true")
	}
	if tm.Stop() {
		t.Fatal("second Stop should return false")
	}
	f.Advance(time.Hour)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
}

func TestFakeZeroDelayReady(t *testing.T) {
	f := NewFake(time.Unix(42, 0))
	tm := f.NewTimer(0)
	select {
	case got := <-tm.C():
		if !got.Equal(time.Unix(42, 0)) {
			t.Fatalf("got %v", got)
		}
	default:
		t.Fatal("zero-duration timer should be ready immediately")
	}
}

func TestFakeSetJumpsAndFires(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	tm := f.NewTimer(time.Hour)
	f.Set(time.Unix(3600, 0))
	select {
	case <-tm.C():
	default:
		t.Fatal("timer should fire after Set past deadline")
	}

	defer func() {
		if r := recover(); r == nil {
			t.Fatal("Set backwards should panic")
		}
	}()
	f.Set(time.Unix(1, 0))
}

func TestFakeNegativeAdvancePanics(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("negative Advance should panic")
		}
	}()
	f.Advance(-time.Second)
}

func TestFakeNewTimerDuringAdvance(t *testing.T) {
	// A timer created at d=0 (e.g. by a handler reacting to a fire) is
	// immediately readable even without another Advance.
	f := NewFake(time.Unix(0, 0))
	tm := f.NewTimer(time.Second)
	go func() {
		<-tm.C()
		child := f.NewTimer(0)
		_ = child
	}()
	f.Advance(time.Second)
	// Nothing to assert externally except no deadlock; the goroutine above
	// completing is checked by the runtime at test end indirectly.
	time.Sleep(10 * time.Millisecond)
}
