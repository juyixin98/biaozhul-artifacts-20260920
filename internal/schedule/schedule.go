// Package schedule 实现“分钟 / 小时 / 周几 + IANA 时区”的定时表达式，
// 以及带明确夏令时（DST）语义的触发时刻枚举。
//
// DST 语义（对表达式命中的每一个本地墙钟时刻）：
//   - 缺失时刻（春季拨快产生的空隙）：该墙钟时间在本地不存在，跳过，不触发；
//   - 重复时刻（秋季拨回产生的重叠）：该墙钟时间在本地出现两次，
//     只在较早的一次（第一次出现）触发；
//   - 以上判定与宿主机时区数据库无关，由调用方传入的 *time.Location 决定。
package schedule

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Expr 是解析后的触发表达式。三个维度之间为“与”关系：
// 仅当本地时间的分钟、小时、周几同时命中时才触发。
type Expr struct {
	Minutes  map[int]bool // 0-59
	Hours    map[int]bool // 0-23
	Weekdays map[int]bool // 0=周日 … 6=周六（与 time.Weekday 一致）

	minutes []int // 升序，供枚举
	hours   []int // 升序，供枚举
}

// Parse 解析三个字段的 cron 风格表达式。
// 每个字段支持：*（全部）、单值 5、列表 1,2,3、区间 1-5、步进 */10 或 1-30/5，
// 以及它们的逗号组合，如 "0,30" 、 "9-18/2"。
// weekdays 取值 0-6（0=周日），另接受 7 表示周日。
func Parse(minutes, hours, weekdays string) (*Expr, error) {
	mins, err := parseField(minutes, 0, 59, "minutes")
	if err != nil {
		return nil, err
	}
	hrs, err := parseField(hours, 0, 23, "hours")
	if err != nil {
		return nil, err
	}
	wds, err := parseField(weekdays, 0, 7, "weekdays")
	if err != nil {
		return nil, err
	}
	wdSet := make(map[int]bool, len(wds))
	for _, w := range wds {
		if w == 7 {
			w = 0 // cron 惯例：7 也是周日
		}
		wdSet[w] = true
	}
	e := &Expr{Minutes: setOf(mins), Hours: setOf(hrs), Weekdays: wdSet, minutes: mins, hours: hrs}
	return e, nil
}

func setOf(v []int) map[int]bool {
	m := make(map[int]bool, len(v))
	for _, x := range v {
		m[x] = true
	}
	return m
}

// parseField 解析单个字段，返回升序去重后的取值列表。
func parseField(spec string, min, max int, name string) ([]int, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return nil, fmt.Errorf("%s: 表达式不能为空", name)
	}
	out := map[int]bool{}
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, fmt.Errorf("%s: 存在空的分段（%q）", name, spec)
		}
		lo, hi, step, err := parseSegment(part, min, max)
		if err != nil {
			return nil, fmt.Errorf("%s: %v", name, err)
		}
		for v := lo; v <= hi; v += step {
			out[v] = true
		}
	}
	vals := make([]int, 0, len(out))
	for v := range out {
		vals = append(vals, v)
	}
	sort.Ints(vals)
	return vals, nil
}

// parseSegment 解析逗号分隔后的一段：[lo[-hi]][/step] 或 *[/step]。
func parseSegment(seg string, min, max int) (lo, hi, step int, err error) {
	step = 1
	base := seg
	if i := strings.Index(seg, "/"); i >= 0 {
		base = seg[:i]
		stepStr := seg[i+1:]
		step, err = strconv.Atoi(stepStr)
		if err != nil || step < 1 {
			return 0, 0, 0, fmt.Errorf("非法步长 %q", seg)
		}
	}
	switch {
	case base == "*":
		lo, hi = min, max
	case strings.Contains(base, "-"):
		parts := strings.SplitN(base, "-", 2)
		v1, err1 := strconv.Atoi(parts[0])
		v2, err2 := strconv.Atoi(parts[1])
		if err1 != nil || err2 != nil {
			return 0, 0, 0, fmt.Errorf("非法区间 %q", seg)
		}
		lo, hi = v1, v2
		if lo > hi {
			return 0, 0, 0, fmt.Errorf("区间下界大于上界 %q", seg)
		}
	default:
		v, err := strconv.Atoi(base)
		if err != nil {
			return 0, 0, 0, fmt.Errorf("非法取值 %q", seg)
		}
		if strings.Contains(seg, "/") {
			// "5/10" 形式：从 5 到上限按步长取值（cron 惯例）
			lo, hi = v, max
		} else {
			lo, hi = v, v
		}
	}
	if lo < min || hi > max {
		return 0, 0, 0, fmt.Errorf("取值超出范围 [%d,%d]: %q", min, max, seg)
	}
	return lo, hi, step, nil
}

// String 返回表达式的规范化文本（调试用）。
func (e *Expr) String() string {
	return fmt.Sprintf("minutes=%v hours=%v weekdays=%v", e.minutes, e.hours, sortedKeys(e.Weekdays))
}

func sortedKeys(m map[int]bool) []int {
	out := make([]int, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Ints(out)
	return out
}

// WallClock 把一个触发瞬间格式化为“本地墙钟”字符串（分钟精度），
// 它是逻辑触发 ID 的组成部分：同一墙钟时刻无论对应几个真实瞬间，
// 字符串都相同，因此天然支撑去重。
func WallClock(t time.Time, loc *time.Location) string {
	return t.In(loc).Format("2006-01-02T15:04")
}

// Between 枚举 [from, to) 内表达式命中的全部触发瞬间（按时间升序）。
//
// from/to 可以是任意时区的瞬间；匹配在 loc 的本地时间轴上进行。
// 最多返回 max 个结果；若实际命中更多，则返回前 max 个且 truncated=true
// （调用方据此实现“补触发上限”）。
//
// 算法：按 loc 的本地日期逐日推进（天然跨年、跨月），对命中周几的每一天，
// 用 resolveLocal 把每个候选墙钟时刻解析为真实瞬间。
func (e *Expr) Between(loc *time.Location, from, to time.Time, max int) (instants []time.Time, truncated bool) {
	if max <= 0 || !to.After(from) {
		return nil, false
	}
	start := from.In(loc)
	y, m, d := start.Date()
	day := time.Date(y, m, d, 0, 0, 0, 0, loc) // 本地当日零点（若零点缺失会被规范化，不影响日期与周几）
	end := to.In(loc)
	for !day.After(end) {
		if e.Weekdays[int(day.Weekday())] {
			dy, dm, dd := day.Date()
			for _, h := range e.hours {
				for _, mi := range e.minutes {
					t, ok := resolveLocal(loc, dy, dm, dd, h, mi)
					if !ok {
						continue // 缺失时刻：跳过
					}
					if t.Before(from) || !t.Before(to) {
						continue
					}
					instants = append(instants, t)
					if len(instants) >= max {
						return instants, true
					}
				}
			}
		}
		day = day.AddDate(0, 0, 1)
	}
	return instants, false
}

// resolveLocal 把 loc 下的本地墙钟时刻 (y,m,d,h:mi) 解析为真实瞬间。
//
// ok=false 表示该墙钟时刻在本地不存在（春季拨快的空隙），调用方应跳过。
// 若该墙钟时刻出现两次（秋季拨回的重叠），返回较早的一次。
//
// 原理：墙钟时刻 W 对应的真实瞬间必为 W-off，其中 off 是 W 附近生效的
// 某个 UTC 偏移。取 W 前后 ±3h 内出现过的全部偏移构造候选瞬间，再用
// “换算回本地墙钟是否仍为 W”逐一验证。该方法对 1 小时及非整小时
// （如 Australia/Lord_Howe 的 30 分钟）的 DST 切换都成立。
func resolveLocal(loc *time.Location, y int, m time.Month, d, h, mi int) (t time.Time, ok bool) {
	base := time.Date(y, m, d, h, mi, 0, 0, loc)
	seen := map[int]bool{}
	var offsets []int
	for _, dt := range []time.Duration{-3 * time.Hour, 0, 3 * time.Hour} {
		_, off := base.Add(dt).Zone()
		if !seen[off] {
			seen[off] = true
			offsets = append(offsets, off)
		}
	}
	wallAsUTC := time.Date(y, m, d, h, mi, 0, 0, time.UTC)
	var best time.Time
	found := false
	for _, off := range offsets {
		cand := wallAsUTC.Add(-time.Duration(off) * time.Second).In(loc)
		cy, cm, cd := cand.Date()
		if cy == y && cm == m && cd == d && cand.Hour() == h && cand.Minute() == mi {
			if !found || cand.Before(best) {
				best, found = cand, true
			}
		}
	}
	return best, found
}
