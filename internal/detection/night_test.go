package detection

import (
	"testing"
	"time"
)

func mustLoc(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Fatalf("load %s: %v", name, err)
	}
	return loc
}

func TestNightLabel_CrossMidnightBoundaries(t *testing.T) {
	loc := mustLoc(t, "Asia/Shanghai")
	day := time.Date(2026, 9, 20, 0, 0, 0, 0, loc)

	cases := []struct {
		name string
		hour int
		min  int
		ok   bool
	}{
		{"19:59 不属于夜间", 19, 59, false},
		{"20:00 恰好开始", 20, 0, true},
		{"23:59 属于夜间", 23, 59, true},
		{"00:00 跨日仍属同一夜", 0, 0, true},
		{"05:59 仍属夜间", 5, 59, true},
		{"06:00 恰好结束", 6, 0, false},
		{"12:00 白天", 12, 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// 00-06 点用“次日”日期构造，20 点之后用当天。
			d := day
			if tc.hour < 12 {
				d = d.AddDate(0, 0, 1)
			}
			tt := time.Date(d.Year(), d.Month(), d.Day(), tc.hour, tc.min, 0, 0, loc)
			label, ok := nightLabel(tt, 20, 6)
			if ok != tc.ok {
				t.Fatalf("ok = %v, want %v (local=%s)", ok, tc.ok, tt)
			}
			if ok && !label.Equal(day) {
				t.Fatalf("label = %s, want %s", label, day)
			}
		})
	}
}

func TestNightWindow_UTCConversion(t *testing.T) {
	loc := mustLoc(t, "Asia/Shanghai") // UTC+8，无 DST
	label := time.Date(2026, 9, 20, 0, 0, 0, 0, loc)
	start, end := nightWindow(label, loc, 20, 6)
	wantStart := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC) // 20:00 +08
	wantEnd := time.Date(2026, 9, 20, 22, 0, 0, 0, time.UTC)   // 次日 06:00 +08
	if !start.Equal(wantStart) || !end.Equal(wantEnd) {
		t.Fatalf("window = [%s, %s), want [%s, %s)", start, end, wantStart, wantEnd)
	}
	if d := end.Sub(start); d != 10*time.Hour {
		t.Fatalf("night length = %s, want 10h", d)
	}
}

func TestNightWindow_DSTFallBack(t *testing.T) {
	// 纽约 2025-11-02 02:00 EDT -> 01:00 EST（时钟回拨 1 小时），
	// 该夜窗口名义 10 小时但实际经过 11 小时：
	// 20:00 EDT(UTC-4) 起 = 11-02 00:00 UTC，次日 06:00 EST(UTC-5) = 11:00 UTC。
	loc := mustLoc(t, "America/New_York")
	label := time.Date(2025, 11, 1, 0, 0, 0, 0, loc)
	start, end := nightWindow(label, loc, 20, 6)
	wantStart := time.Date(2025, 11, 2, 0, 0, 0, 0, time.UTC)
	wantEnd := time.Date(2025, 11, 2, 11, 0, 0, 0, time.UTC)
	if !start.Equal(wantStart) || !end.Equal(wantEnd) {
		t.Fatalf("fallback window = [%s, %s), want [%s, %s)", start, end, wantStart, wantEnd)
	}
	if d := end.Sub(start); d != 11*time.Hour {
		t.Fatalf("fallback night length = %s, want 11h", d)
	}
}

func TestNightWindow_DSTSpringForward(t *testing.T) {
	// 纽约 2026-03-08 02:00 -> 03:00（时钟前跳 1 小时），
	// 跨越切换的是 3 月 7 日标签夜：20:00 EST(UTC-5) 起 = 03-08 01:00 UTC，
	// 次日 06:00 EDT(UTC-4) = 10:00 UTC，实际经过 9 小时。
	loc := mustLoc(t, "America/New_York")
	label := time.Date(2026, 3, 7, 0, 0, 0, 0, loc)
	start, end := nightWindow(label, loc, 20, 6)
	wantStart := time.Date(2026, 3, 8, 1, 0, 0, 0, time.UTC)
	wantEnd := time.Date(2026, 3, 8, 10, 0, 0, 0, time.UTC)
	if !start.Equal(wantStart) || !end.Equal(wantEnd) {
		t.Fatalf("spring window = [%s, %s), want [%s, %s)", start, end, wantStart, wantEnd)
	}
	if d := end.Sub(start); d != 9*time.Hour {
		t.Fatalf("spring night length = %s, want 9h", d)
	}
}

func TestAffectedNightLabels(t *testing.T) {
	loc := mustLoc(t, "Asia/Shanghai")
	label := time.Date(2026, 9, 20, 0, 0, 0, 0, loc)
	start, end := nightWindow(label, loc, 20, 6)

	// 仅覆盖窗口前 10 分钟，也应只命中一个标签。
	got := affectedNightLabels(start, start.Add(10*time.Minute), loc, 20, 6)
	if len(got) != 1 || !got[0].Equal(label) {
		t.Fatalf("labels = %v, want [%s]", got, label)
	}

	// 跨两夜的区间命中两个标签。
	got = affectedNightLabels(start, end.Add(24*time.Hour+time.Hour), loc, 20, 6)
	if len(got) != 2 {
		t.Fatalf("labels = %v, want 2 labels", got)
	}

	// 白天区间不命中任何标签。
	day := time.Date(2026, 9, 22, 4, 0, 0, 0, time.UTC) // 上海 12:00
	got = affectedNightLabels(day, day.Add(time.Hour), loc, 20, 6)
	if len(got) != 0 {
		t.Fatalf("daytime labels = %v, want none", got)
	}
}
