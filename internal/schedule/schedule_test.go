package schedule

import (
	"testing"
	"time"

	"tztrigger/internal/tzdb"
)

func mustParse(t *testing.T, mins, hrs, wds string) *Expr {
	t.Helper()
	e, err := Parse(mins, hrs, wds)
	if err != nil {
		t.Fatalf("Parse(%q,%q,%q) 出错: %v", mins, hrs, wds, err)
	}
	return e
}

func mustLoc(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := tzdb.LoadLocation(name)
	if err != nil {
		t.Fatalf("加载时区 %s 失败: %v", name, err)
	}
	return loc
}

func utc(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestParseValid(t *testing.T) {
	cases := []struct {
		mins, hrs, wds string
		wantMins       int // 分钟集合大小
		wantWds        []int
	}{
		{"*", "*", "*", 60, []int{0, 1, 2, 3, 4, 5, 6}},
		{"0", "9", "1-5", 1, []int{1, 2, 3, 4, 5}},
		{"0,30", "9-18/2", "1,3,5", 2, []int{1, 3, 5}},
		{"*/15", "0", "0", 4, []int{0}},
		{"5/20", "0", "7", 3, []int{0}}, // 5,25,45；7=周日
		{"0-59", "0-23", "0-6", 60, []int{0, 1, 2, 3, 4, 5, 6}},
	}
	for _, c := range cases {
		e := mustParse(t, c.mins, c.hrs, c.wds)
		if len(e.Minutes) != c.wantMins {
			t.Errorf("Parse(%q,...): 分钟数=%d, 期望 %d", c.mins, len(e.Minutes), c.wantMins)
		}
		for _, w := range c.wantWds {
			if !e.Weekdays[w] {
				t.Errorf("Parse(weekdays=%q): 缺少周 %d", c.wds, w)
			}
		}
	}
}

func TestParseInvalid(t *testing.T) {
	cases := [][3]string{
		{"", "*", "*"},
		{"60", "*", "*"},
		{"-1", "*", "*"},
		{"*", "24", "*"},
		{"*", "*", "8"},
		{"a", "*", "*"},
		{"1-", "*", "*"},
		{"5-1", "*", "*"},
		{"*/0", "*", "*"},
		{"1,,2", "*", "*"},
		{"* / x", "*", "*"},
	}
	for _, c := range cases {
		if _, err := Parse(c[0], c[1], c[2]); err == nil {
			t.Errorf("Parse(%q,%q,%q) 应报错但没有", c[0], c[1], c[2])
		}
	}
}

// 2026 年美国东部春季切换：2026-03-08 02:00 时钟拨到 03:00，
// 本地 02:00–02:59 不存在（tzdata 2024b，美国规则自 2007 年未变）。
func TestSpringForwardGapSkipped(t *testing.T) {
	loc := mustLoc(t, "America/New_York")
	e := mustParse(t, "30", "2", "*") // 每天 02:30
	from := utc("2026-03-07T00:00:00Z")
	to := utc("2026-03-10T00:00:00Z")
	occ, trunc := e.Between(loc, from, to, 100)
	if trunc {
		t.Fatal("不应截断")
	}
	// 03-07 与 03-09 的 02:30 存在，03-08 的 02:30 缺失被跳过
	want := []string{"2026-03-07T07:30:00Z", "2026-03-09T06:30:00Z"}
	if len(occ) != len(want) {
		t.Fatalf("命中 %d 次 %v，期望 %d 次 %v", len(occ), occ, len(want), want)
	}
	for i, w := range want {
		if got := occ[i].UTC().Format(time.RFC3339); got != w {
			t.Errorf("第 %d 次 = %s，期望 %s", i, got, w)
		}
	}
	// 确认没有任何事件落在 03-08 本地
	for _, o := range occ {
		if o.In(loc).Day() == 8 && o.In(loc).Month() == time.March {
			t.Errorf("缺失日 03-08 不应有触发: %v", o.In(loc))
		}
	}
}

// 2026 年美国东部秋季切换：2026-11-01 02:00 拨回 01:00，
// 本地 01:00–01:59 出现两次。01:30 的两次出现分别是
// 05:30 UTC（EDT，较早）与 06:30 UTC（EST，较晚）。
func TestFallBackEarlierOccurrenceOnly(t *testing.T) {
	loc := mustLoc(t, "America/New_York")
	e := mustParse(t, "30", "1", "*") // 每天 01:30
	from := utc("2026-10-31T00:00:00Z")
	to := utc("2026-11-03T00:00:00Z")
	occ, _ := e.Between(loc, from, to, 100)
	want := []string{
		"2026-10-31T05:30:00Z", // 10-31 01:30 EDT
		"2026-11-01T05:30:00Z", // 11-01 01:30 第一次（EDT，较早）
		"2026-11-02T06:30:00Z", // 11-02 01:30 EST
	}
	if len(occ) != len(want) {
		t.Fatalf("命中 %d 次 %v，期望 %d 次 %v", len(occ), occ, len(want), want)
	}
	for i, w := range want {
		if got := occ[i].UTC().Format(time.RFC3339); got != w {
			t.Errorf("第 %d 次 = %s，期望 %s（重复时刻只执行较早一次）", i, got, w)
		}
	}
	// 11-01 本地 01:30 只能出现一次
	count := 0
	for _, o := range occ {
		l := o.In(loc)
		if l.Month() == time.November && l.Day() == 1 && l.Hour() == 1 && l.Minute() == 30 {
			count++
		}
	}
	if count != 1 {
		t.Errorf("11-01 01:30 触发了 %d 次，期望恰好 1 次", count)
	}
}

// 非整小时 DST：Australia/Lord_Howe 偏移变化为 30 分钟。
// 2026-10-04 02:00→02:30（春季，本地 02:00–02:29 缺失）；
// 2026-04-05 02:00→01:30（秋季，本地 01:30–01:59 重复）。
func TestHalfHourDST(t *testing.T) {
	loc := mustLoc(t, "Australia/Lord_Howe")

	// 春季：02:15 不存在，应跳过
	e := mustParse(t, "15", "2", "*")
	occ, _ := e.Between(loc, utc("2026-10-03T00:00:00Z"), utc("2026-10-06T00:00:00Z"), 100)
	for _, o := range occ {
		l := o.In(loc)
		if l.Day() == 4 && l.Month() == time.October {
			t.Errorf("Lord Howe 2026-10-04 02:15 缺失，不应触发: %v", l)
		}
	}
	if len(occ) != 2 { // 10-03 与 10-05
		t.Errorf("Lord Howe 春季命中 %d 次，期望 2 次", len(occ))
	}

	// 秋季：01:45 出现两次，取较早一次（+11 偏移侧）
	e2 := mustParse(t, "45", "1", "*")
	occ2, _ := e2.Between(loc, utc("2026-04-04T00:00:00Z"), utc("2026-04-07T00:00:00Z"), 100)
	// 04-05 01:45 第一次 = 04-04 14:45 UTC（+11）
	want := "2026-04-04T14:45:00Z"
	found := false
	for _, o := range occ2 {
		l := o.In(loc)
		if l.Month() == time.April && l.Day() == 5 {
			if got := o.UTC().Format(time.RFC3339); got != want {
				t.Errorf("Lord Howe 秋季 01:45 = %s，期望较早一次 %s", got, want)
			}
			found = true
		}
	}
	if !found {
		t.Error("Lord Howe 2026-04-05 01:45 应触发一次")
	}
	if len(occ2) != 3 { // 04-04、04-05、04-06 各一次
		t.Errorf("Lord Howe 秋季窗口命中 %d 次，期望 3 次", len(occ2))
	}
}

// 跨年：周几匹配在 12 月/1 月边界正确推进。
// 2027-01-04 是周一；2026-12-28 也是周一。
func TestCrossYearWeekday(t *testing.T) {
	loc := mustLoc(t, "UTC")
	e := mustParse(t, "0", "0", "1") // 每周一 00:00
	occ, _ := e.Between(loc, utc("2026-12-30T00:00:00Z"), utc("2027-01-06T00:00:00Z"), 100)
	if len(occ) != 1 {
		t.Fatalf("跨年窗口命中 %d 次，期望 1 次（2027-01-04 周一）", len(occ))
	}
	if got := occ[0].UTC().Format(time.RFC3339); got != "2027-01-04T00:00:00Z" {
		t.Errorf("跨年周一 = %s，期望 2027-01-04T00:00:00Z", got)
	}

	// 每日表达式跨年计数：2026-12-01 至 2027-02-01 共 62 天
	daily := mustParse(t, "0", "9", "*")
	occ2, trunc := daily.Between(loc, utc("2026-12-01T00:00:00Z"), utc("2027-02-01T00:00:00Z"), 100)
	if trunc || len(occ2) != 62 {
		t.Errorf("跨年每日命中 %d 次 (trunc=%v)，期望 62 次", len(occ2), trunc)
	}
	// 首尾检查
	if got := occ2[0].UTC().Format(time.RFC3339); got != "2026-12-01T09:00:00Z" {
		t.Errorf("首次 = %s", got)
	}
	if got := occ2[61].UTC().Format(time.RFC3339); got != "2027-01-31T09:00:00Z" {
		t.Errorf("末次 = %s", got)
	}
}

// Between 的截断标志供补触发上限使用。
func TestBetweenTruncation(t *testing.T) {
	loc := mustLoc(t, "UTC")
	e := mustParse(t, "*", "*", "*") // 每分钟
	occ, trunc := e.Between(loc, utc("2026-01-01T00:00:00Z"), utc("2026-01-02T00:00:00Z"), 10)
	if !trunc {
		t.Error("应报告截断")
	}
	if len(occ) != 10 {
		t.Errorf("截断后返回 %d 个，期望 10", len(occ))
	}
	// 升序
	for i := 1; i < len(occ); i++ {
		if !occ[i].After(occ[i-1]) {
			t.Error("结果应严格升序")
		}
	}
}

// 半开区间 [from, to)：from 处命中包含，to 处命中排除。
func TestBetweenHalfOpen(t *testing.T) {
	loc := mustLoc(t, "UTC")
	e := mustParse(t, "0", "0", "*")
	occ, _ := e.Between(loc, utc("2026-01-01T00:00:00Z"), utc("2026-01-03T00:00:00Z"), 100)
	if len(occ) != 2 {
		t.Fatalf("半开区间命中 %d 次，期望 2 次", len(occ))
	}
	if occ[0].UTC().Format(time.RFC3339) != "2026-01-01T00:00:00Z" {
		t.Errorf("from 边界应包含: %v", occ[0])
	}
}
