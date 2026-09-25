package scheduler

import "testing"

func TestIntervalHalfOpen(t *testing.T) {
	cases := []struct {
		name string
		a, b Interval
		want bool
	}{
		{"完全相同", Interval{1, 3}, Interval{1, 3}, true},
		{"部分重叠", Interval{1, 4}, Interval{3, 5}, true},
		{"a 包含 b", Interval{0, 10}, Interval{2, 4}, true},
		{"相邻 a 在前（关键）", Interval{1, 3}, Interval{3, 5}, false},
		{"相邻 b 在前（关键）", Interval{3, 5}, Interval{1, 3}, false},
		{"完全分离", Interval{1, 2}, Interval{3, 4}, false},
		{"端点接触于同一点", Interval{0, 100}, Interval{100, 200}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.a.Overlaps(c.b); got != c.want {
				t.Fatalf("Overlaps(%v,%v)=%v，期望 %v", c.a, c.b, got, c.want)
			}
			// 对称性
			if c.a.Overlaps(c.b) != c.b.Overlaps(c.a) {
				t.Fatalf("重叠关系不对称: %v %v", c.a, c.b)
			}
		})
	}
}

func TestIntervalContains(t *testing.T) {
	iv := Interval{3, 5}
	if !iv.Contains(3) {
		t.Fatal("左端点应属于区间（左闭）")
	}
	if iv.Contains(5) {
		t.Fatal("右端点不应属于区间（右开）")
	}
	if !iv.Contains(4) {
		t.Fatal("内部点应属于区间")
	}
}

func TestIntervalValidate(t *testing.T) {
	if err := (Interval{1, 1}).Validate(); err == nil {
		t.Fatal("零长度区间必须非法")
	}
	if err := (Interval{2, 1}).Validate(); err == nil {
		t.Fatal("负长度区间必须非法")
	}
	if err := (Interval{0, 1}).Validate(); err != nil {
		t.Fatalf("正常区间不应报错: %v", err)
	}
}
