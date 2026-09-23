package model

import "testing"

func TestParseTs(t *testing.T) {
	cases := []struct {
		in   string
		want int64
		ok   bool
	}{
		{"1735693200", 1735693200, true},
		{"2025-01-01T01:00:00Z", 1735693200, true},
		{"2025-01-01T02:30:00+01:00", 1735695000, true},
		{"", 0, false},
		{"not-a-time", 0, false},
		{"2025-13-99T99:99:99Z", 0, false},
	}
	for _, c := range cases {
		got, err := ParseTs(c.in)
		if c.ok {
			if err != nil || got != c.want {
				t.Errorf("ParseTs(%q) = %d, %v; 期望 %d", c.in, got, err, c.want)
			}
		} else if err == nil {
			t.Errorf("ParseTs(%q) 应报错，却得到 %d", c.in, got)
		}
	}
}

func TestFloorToWindow(t *testing.T) {
	cases := []struct {
		ts, window, want int64
	}{
		{0, 60, 0},
		{59, 60, 0},
		{60, 60, 60},
		{3599, 3600, 0},
		{3600, 3600, 3600},
		{3661, 3600, 3600},
		{100, 0, 100}, // window=0 原样返回
	}
	for _, c := range cases {
		if got := FloorToWindow(c.ts, c.window); got != c.want {
			t.Errorf("FloorToWindow(%d,%d)=%d 期望 %d", c.ts, c.window, got, c.want)
		}
	}
}

func TestSeriesKeyStable(t *testing.T) {
	a := Sample{Metric: "m", Labels: map[string]string{"a": "1", "b": "2"}}
	b := Sample{Metric: "m", Labels: map[string]string{"b": "2", "a": "1"}}
	if a.Key() != b.Key() {
		t.Fatal("标签顺序不同应得到相同序列键")
	}
	c := Sample{Metric: "m", Labels: map[string]string{"a": "1", "b": "3"}}
	if a.Key() == c.Key() {
		t.Fatal("标签值不同应得到不同序列键")
	}
}
