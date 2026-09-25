package clock

import (
	"testing"
	"time"
)

func TestFakeClockAdvance(t *testing.T) {
	start := time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC)
	c := NewFake(start)
	if !c.Now().Equal(start) {
		t.Fatalf("初始时间错误: %v", c.Now())
	}
	c.Advance(5 * time.Minute)
	if !c.Now().Equal(start.Add(5 * time.Minute)) {
		t.Fatalf("推进后时间错误: %v", c.Now())
	}
	c.Set(start)
	if !c.Now().Equal(start) {
		t.Fatalf("Set 后时间错误: %v", c.Now())
	}
}

func TestRealClockMoves(t *testing.T) {
	t1 := (Real{}).Now()
	time.Sleep(time.Millisecond)
	if !(Real{}).Now().After(t1) {
		t.Fatal("真实时钟应当前进")
	}
}
