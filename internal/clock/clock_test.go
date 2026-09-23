package clock_test

import (
	"testing"
	"time"

	"worksteal/internal/clock"
)

func TestFakeTimerOrdering(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	f := clock.NewFake(start)
	t1 := f.NewTimer(10 * time.Second)
	t2 := f.NewTimer(5 * time.Second)
	t3 := f.NewTimer(5 * time.Second)

	// 推进 4s：无触发。
	f.Advance(4 * time.Second)
	if f.Pending() != 3 {
		t.Fatalf("pending=%d want 3", f.Pending())
	}
	assertNoFire(t, t1.C())
	assertNoFire(t, t2.C())

	// 推进到 9s：t2、t3（计划在 +5s，同刻按注册顺序）触发。
	f.Advance(5 * time.Second)
	waitFire(t, t2.C(), start.Add(5*time.Second))
	waitFire(t, t3.C(), start.Add(5*time.Second))
	assertNoFire(t, t1.C())

	// 推进到 10s：t1 触发。
	f.Advance(1 * time.Second)
	waitFire(t, t1.C(), start.Add(10*time.Second))
}

func TestFakeTimerStop(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	tm := f.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("first Stop should return true")
	}
	f.Advance(time.Hour)
	assertNoFire(t, tm.C())
	if tm.Stop() {
		t.Fatal("second Stop should return false")
	}
}

func TestFakeZeroAndNegativeDelay(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	tm := f.NewTimer(0)
	f.Advance(0)
	waitFire(t, tm.C(), time.Unix(0, 0))

	base := time.Unix(100, 0)
	f2 := clock.NewFake(base)
	tm2 := f2.NewTimer(-5 * time.Second)
	f2.Advance(0)
	// 负时长被夹到创建时刻立即触发。
	waitFire(t, tm2.C(), base)
}

func TestRealClock(t *testing.T) {
	c := clock.NewReal()
	before := time.Now()
	tm := c.NewTimer(5 * time.Millisecond)
	<-tm.C()
	if time.Since(before) < 4*time.Millisecond {
		t.Fatal("timer fired too early")
	}
	if tm.Stop() {
		t.Fatal("Stop after fire should be false")
	}
}

func waitFire(t *testing.T, ch <-chan time.Time, want time.Time) {
	t.Helper()
	select {
	case got := <-ch:
		if !got.Equal(want) {
			t.Fatalf("fired at %v want %v", got, want)
		}
	default:
		t.Fatal("expected timer to have fired")
	}
}

func assertNoFire(t *testing.T, ch <-chan time.Time) {
	t.Helper()
	select {
	case v := <-ch:
		t.Fatalf("unexpected fire at %v", v)
	default:
	}
}
