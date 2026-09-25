package clock

import (
	"testing"
	"time"
)

func TestFakeClockSetAdvance(t *testing.T) {
	t0 := time.Date(2030, 1, 1, 12, 0, 0, 0, time.UTC)
	fc := NewFake(t0)
	if !fc.Now().Equal(t0) {
		t.Fatalf("初始时刻错误: %v", fc.Now())
	}
	fc.Advance(5 * time.Minute)
	if want := t0.Add(5 * time.Minute); !fc.Now().Equal(want) {
		t.Fatalf("推进后期望 %v，得到 %v", want, fc.Now())
	}
	t1 := time.Date(2031, 6, 1, 0, 0, 0, 0, time.UTC)
	fc.Set(t1)
	if !fc.Now().Equal(t1) {
		t.Fatalf("Set 后期望 %v，得到 %v", t1, fc.Now())
	}
}

func TestWallClock(t *testing.T) {
	before := time.Now().UTC()
	got := Wall{}.Now()
	after := time.Now().UTC()
	if got.Before(before) || got.After(after) {
		t.Fatalf("墙钟 %v 不在 [%v,%v] 内", got, before, after)
	}
	if got.Location() != time.UTC {
		t.Fatalf("墙钟应统一 UTC，得到 %v", got.Location())
	}
}
