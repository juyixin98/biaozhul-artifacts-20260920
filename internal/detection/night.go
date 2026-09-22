package detection

import "time"

// nightLabel 返回 t（应已转换到指定时区）所在夜间窗口的“标签日期”：
// 夜间窗口为当地 [startHour:00, 次日 endHour:00)，标签为起始那一天的日期。
// 若 t 不在夜间时段，返回 ok=false。
//
// 例：startHour=20,endHour=6
//
//	19:59 -> 非夜间
//	20:00 -> 当天
//	00:30 -> 前一天
//	05:59 -> 前一天
//	06:00 -> 非夜间
func nightLabel(local time.Time, startHour, endHour int) (time.Time, bool) {
	date := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, local.Location())
	h := local.Hour()
	// 跨夜（startHour > endHour，如 20 -> 6）
	if startHour > endHour {
		if h >= startHour {
			return date, true
		}
		if h < endHour {
			return date.AddDate(0, 0, -1), true
		}
		return time.Time{}, false
	}
	// 不跨夜的通用情况（如 22 -> 23），当前需求用不到，保留正确语义。
	if h >= startHour && h < endHour {
		return date, true
	}
	return time.Time{}, false
}

// nightWindow 返回标签日期 label（loc 时区当日 00:00）对应夜间窗口的 UTC 起止。
func nightWindow(label time.Time, loc *time.Location, startHour, endHour int) (startUTC, endUTC time.Time) {
	start := time.Date(label.Year(), label.Month(), label.Day(), startHour, 0, 0, 0, loc)
	endDay := label.AddDate(0, 0, 1)
	end := time.Date(endDay.Year(), endDay.Month(), endDay.Day(), endHour, 0, 0, 0, loc)
	return start.UTC(), end.UTC()
}

// affectedNightLabels 返回与 UTC 区间 [from, to] 有交集的全部夜间标签日期（按 loc）。
func affectedNightLabels(from, to time.Time, loc *time.Location, startHour, endHour int) []time.Time {
	// 候选标签从 from 当地日期前一天（覆盖跨夜窗口的起点）枚举到 to 当地日期。
	startLocal := from.In(loc)
	endLocal := to.In(loc)
	first := time.Date(startLocal.Year(), startLocal.Month(), startLocal.Day(), 0, 0, 0, 0, loc).AddDate(0, 0, -1)
	last := time.Date(endLocal.Year(), endLocal.Month(), endLocal.Day(), 0, 0, 0, 0, loc)

	var out []time.Time
	for d := first; !d.After(last); d = d.AddDate(0, 0, 1) {
		ws, we := nightWindow(d, loc, startHour, endHour)
		if we.After(from) && ws.Before(to) {
			out = append(out, d)
		}
	}
	return out
}

// dayRangeUTC 返回 loc 时区日期 d 当天 [00:00, 次日 00:00) 的 UTC 时间。
func dayRangeUTC(d time.Time, loc *time.Location) (time.Time, time.Time) {
	start := time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, loc)
	end := start.AddDate(0, 0, 1)
	return start.UTC(), end.UTC()
}

// localDate 把 UTC 时间转 loc 并归零到当日 00:00。
func localDate(t time.Time, loc *time.Location) time.Time {
	l := t.In(loc)
	return time.Date(l.Year(), l.Month(), l.Day(), 0, 0, 0, 0, loc)
}
