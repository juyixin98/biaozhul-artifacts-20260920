package integration_test

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"activityguard/internal/models"
	"activityguard/internal/testsupport"
)

// TestOutOfOrderBackfill 验证乱序/延迟上报：
// 先到一条较晚 USB，再补录更早 USB，首次 USB 告警始终只有一条，
// 且依据最终锁定在“最早的 USB 事件”。
func TestOutOfOrderBackfill(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()
	emp := env.EmployeeByEmail(t, "carla.rossi@example.com") // Europe/Rome

	base := time.Date(2025, 12, 1, 12, 0, 0, 0, time.UTC)
	later := testsupport.NewEvent("oot-usb-later", models.EventUSB, emp.ID, base.Add(2*time.Hour),
		map[string]any{"vendor": "LaterVendor", "serial": "L"})
	if _, err := eng.ProcessEvents([]models.Event{later}); err != nil {
		t.Fatalf("process later: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleFirstUSB); n != 1 {
		t.Fatalf("first usb alerts after later = %d, want 1", n)
	}

	// 延迟 2 小时前的更早事件到达（补算）。
	earlier := testsupport.NewEvent("oot-usb-earlier", models.EventUSB, emp.ID, base,
		map[string]any{"vendor": "FirstVendor", "serial": "F"})
	if _, err := eng.ProcessEvents([]models.Event{earlier}); err != nil {
		t.Fatalf("process earlier: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleFirstUSB); n != 1 {
		t.Fatalf("first usb alerts after backfill = %d, want 1 (no duplicate)", n)
	}

	alerts := env.AlertsFor(t, emp.ID, models.RuleFirstUSB)
	if len(alerts) != 1 {
		t.Fatalf("alerts = %d", len(alerts))
	}
	a := alerts[0]
	if !a.FirstSeenAt.Equal(base) {
		t.Fatalf("first_seen_at = %s, want earlier event %s", a.FirstSeenAt, base)
	}
	if a.RuleVersion != 1 {
		t.Fatalf("rule_version = %d, want 1", a.RuleVersion)
	}
	if !strings.Contains(string(a.Evidence), "oot-usb-earlier") {
		t.Fatalf("evidence should reference earliest event: %s", a.Evidence)
	}
}

// TestDownloadBurstAndIdempotentRecompute 验证 10 分钟 >50 下载触发，
// 且对同一窗口重复检测不会产生重复告警。
func TestDownloadBurstAndIdempotentRecompute(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()
	emp := env.EmployeeByEmail(t, "frank.zhao@example.com") // UTC

	bucket := time.Date(2025, 12, 10, 13, 0, 0, 0, time.UTC)
	evs := make([]models.Event, 0, 51)
	for i := 0; i < 51; i++ {
		evs = append(evs, testsupport.NewEvent(
			"burst-"+pad3(i), models.EventFileDownload, emp.ID,
			bucket.Add(time.Duration(i)*5*time.Second),
			map[string]any{"file": "f.dat"},
		))
	}
	if _, err := eng.ProcessEvents(evs); err != nil {
		t.Fatalf("process: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleDownloadBurst); n != 1 {
		t.Fatalf("burst alerts = %d, want 1", n)
	}
	a := env.AlertsFor(t, emp.ID, models.RuleDownloadBurst)[0]
	if jsonCount(t, a.Evidence) != 51 {
		t.Fatalf("evidence count wrong: %s", a.Evidence)
	}

	// 对同样事件再跑一轮（等价重复调度），不新增告警且依据刷新为 52。
	extra := testsupport.NewEvent("burst-extra", models.EventFileDownload, emp.ID,
		bucket.Add(9*time.Minute), map[string]any{"file": "g.dat"})
	if _, err := eng.ProcessEvents([]models.Event{extra}); err != nil {
		t.Fatalf("recompute: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleDownloadBurst); n != 1 {
		t.Fatalf("burst alerts after recompute = %d, want 1", n)
	}
	a = env.AlertsFor(t, emp.ID, models.RuleDownloadBurst)[0]
	if jsonCount(t, a.Evidence) != 52 {
		t.Fatalf("evidence should refresh to 52: %s", a.Evidence)
	}
}

func jsonCount(t *testing.T, raw []byte) int {
	t.Helper()
	var m struct {
		Count int `json:"count"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse evidence %s: %v", raw, err)
	}
	return m.Count
}

func jsonFieldInt(t *testing.T, raw []byte, field string) int {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse evidence: %v", err)
	}
	v, _ := m[field].(float64)
	return int(v)
}

// TestNightTimezoneBoundaries 验证跨日与夏令时边界：
// 上海 20:00/06:00 精确边界，以及纽约 DST 回拨夜实际经过 11 小时。
func TestNightTimezoneBoundaries(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()

	t.Run("Shanghai 精确 20:00 触发且 06:00 不触发", func(t *testing.T) {
		emp := env.EmployeeByEmail(t, "bob.li@example.com") // Asia/Shanghai
		loc, _ := time.LoadLocation(emp.Timezone)
		day := time.Date(2025, 12, 15, 0, 0, 0, 0, loc)

		at2000 := time.Date(2025, 12, 15, 20, 0, 0, 0, loc)
		at0600 := time.Date(2025, 12, 16, 6, 0, 0, 0, loc) // 同一夜窗口的结束边界
		evs := []models.Event{
			testsupport.NewEvent("sh-2000", models.EventLogin, emp.ID, at2000, nil),
			testsupport.NewEvent("sh-0600", models.EventLogin, emp.ID, at0600, nil),
		}
		if _, err := eng.ProcessEvents(evs); err != nil {
			t.Fatalf("process: %v", err)
		}
		alerts := env.AlertsFor(t, emp.ID, models.RuleNightActivity)
		if len(alerts) != 1 {
			t.Fatalf("night alerts = %d, want 1 (20:00 only, 06:00 excluded)", len(alerts))
		}
		if !alerts[0].WindowStart.Equal(at2000.UTC()) {
			t.Fatalf("window start = %s, want %s", alerts[0].WindowStart, at2000.UTC())
		}
		if jsonFieldInt(t, alerts[0].Evidence, "event_count") != 1 {
			t.Fatalf("evidence should contain exactly 1 event: %s", alerts[0].Evidence)
		}
		_ = day
	})

	t.Run("New_York DST 回拨夜为 11 小时且跨夜标签不重复", func(t *testing.T) {
		emp := env.EmployeeByEmail(t, "alice.chen@example.com") // America/New_York

		// 回拨夜存在“重名本地时间”，Go 对模糊本地时取第一次（EDT），
		// 故事件直接用无歧义 UTC 时刻构造：
		// 20:00 EDT = 00:00Z；回拨后 01:30 EST = 06:30Z；05:59 EST = 10:59Z。
		nightStart := time.Date(2025, 11, 2, 0, 0, 0, 0, time.UTC)
		afterFall := time.Date(2025, 11, 2, 6, 30, 0, 0, time.UTC)
		nearEnd := time.Date(2025, 11, 2, 10, 59, 0, 0, time.UTC)
		evs := []models.Event{
			testsupport.NewEvent("nyc-2000", models.EventLogin, emp.ID, nightStart, nil),
			testsupport.NewEvent("nyc-fallback", models.EventUSB, emp.ID, afterFall, map[string]any{"serial": "fb"}),
			testsupport.NewEvent("nyc-0559", models.EventLogin, emp.ID, nearEnd, nil),
		}
		if _, err := eng.ProcessEvents(evs); err != nil {
			t.Fatalf("process: %v", err)
		}
		alerts := env.AlertsFor(t, emp.ID, models.RuleNightActivity)
		if len(alerts) != 1 {
			t.Fatalf("night alerts across DST fallback = %d, want 1 label", len(alerts))
		}
		a := alerts[0]
		wantStart := time.Date(2025, 11, 2, 0, 0, 0, 0, time.UTC)
		wantEnd := time.Date(2025, 11, 2, 11, 0, 0, 0, time.UTC)
		if !a.WindowStart.Equal(wantStart) || !a.WindowEnd.Equal(wantEnd) {
			t.Fatalf("DST window = [%s,%s), want [%s,%s)", a.WindowStart, a.WindowEnd, wantStart, wantEnd)
		}
		if d := a.WindowEnd.Sub(*a.WindowStart); d != 11*time.Hour {
			t.Fatalf("night length = %s, want 11h (fallback night)", d)
		}
		if jsonFieldInt(t, a.Evidence, "event_count") != 3 {
			t.Fatalf("evidence count = %s", a.Evidence)
		}
	})
}

// TestNightSplitBatchesFullEvidence 同一夜窗口的事件分两批到达：
// 重算后的 evidence 必须覆盖该夜全部事件，而不是只含后到的批次。
func TestNightSplitBatchesFullEvidence(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()
	emp := env.EmployeeByEmail(t, "frank.zhao@example.com") // UTC

	night := time.Date(2025, 12, 20, 22, 0, 0, 0, time.UTC)
	batch1 := []models.Event{
		testsupport.NewEvent("split-n1", models.EventLogin, emp.ID, night, nil),
	}
	batch2 := []models.Event{
		testsupport.NewEvent("split-n2", models.EventFileDownload, emp.ID, night.Add(3*time.Hour), map[string]any{"file": "x"}),
		testsupport.NewEvent("split-n3", models.EventUSB, emp.ID, night.Add(4*time.Hour), map[string]any{"serial": "s"}),
	}
	if _, err := eng.ProcessEvents(batch1); err != nil {
		t.Fatalf("batch1: %v", err)
	}
	if _, err := eng.ProcessEvents(batch2); err != nil {
		t.Fatalf("batch2: %v", err)
	}
	alerts := env.AlertsFor(t, emp.ID, models.RuleNightActivity)
	if len(alerts) != 1 {
		t.Fatalf("night alerts = %d, want 1", len(alerts))
	}
	if got := jsonFieldInt(t, alerts[0].Evidence, "event_count"); got != 3 {
		t.Fatalf("event_count after split batches = %d, want 3 (full window)", got)
	}
}

// TestZScoreInsufficientSampleAndAnomaly 覆盖：
// 样本不足时不出统计告警；积累足够样本后，当日突增触发且只有一条。
func TestZScoreInsufficientSampleAndAnomaly(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()
	emp := env.EmployeeByEmail(t, "emma.wang@example.com") // America/Los_Angeles
	loc, _ := time.LoadLocation(emp.Timezone)

	// 只有 3 天历史 -> 样本不足（min_sample_days=7），即便当日很多也只用固定规则。
	now := time.Now().UTC()
	today := midnightInLoc(now, loc)
	evs := []models.Event{}
	for d := 3; d >= 1; d-- {
		day := today.AddDate(0, 0, -d)
		for i := 0; i < 2; i++ {
			evs = append(evs, testsupport.NewEvent(
				"zs-short-"+pad3(d)+"-"+pad3(i), models.EventFileDownload, emp.ID,
				day.Add(10*time.Hour+time.Duration(i)*time.Minute), nil))
		}
	}
	for i := 0; i < 30; i++ {
		evs = append(evs, testsupport.NewEvent("zs-short-now-"+pad3(i),
			models.EventFileDownload, emp.ID,
			today.Add(9*time.Hour+time.Duration(i)*time.Minute), nil))
	}
	// 30 个下载分散在 30 分钟以上，并落在不同 10 分钟桶，避免触发 burst 规则。
	if _, err := eng.ProcessEvents(evs); err != nil {
		t.Fatalf("process: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleZScore); n != 0 {
		t.Fatalf("zscore alerts with insufficient sample = %d, want 0", n)
	}

	// 补足 10 天稳定历史（每天 2 个），再重算当日。
	more := []models.Event{}
	for d := 10; d >= 4; d-- {
		day := today.AddDate(0, 0, -d)
		for i := 0; i < 2; i++ {
			more = append(more, testsupport.NewEvent(
				"zs-base-"+pad3(d)+"-"+pad3(i), models.EventFileDownload, emp.ID,
				day.Add(11*time.Hour+time.Duration(i)*time.Minute), nil))
		}
	}
	// 用一个“新到达”的当日事件触发受影响窗口重算（覆盖今日及其后历史样本日）。
	trigger := testsupport.NewEvent("zs-trigger", models.EventFileDownload, emp.ID,
		today.Add(14*time.Hour), nil)
	more = append(more, trigger)
	if _, err := eng.ProcessEvents(more); err != nil {
		t.Fatalf("process more: %v", err)
	}
	if n := env.CountAlerts(t, models.RuleZScore); n != 1 {
		t.Fatalf("zscore alerts with adequate sample = %d, want 1", n)
	}
	a := env.AlertsFor(t, emp.ID, models.RuleZScore)[0]
	if !strings.Contains(string(a.Evidence), `"z":`) || strings.Contains(string(a.Evidence), `"triggered":false`) {
		t.Fatalf("zscore evidence malformed: %s", a.Evidence)
	}
}

func midnightInLoc(t time.Time, loc *time.Location) time.Time {
	l := t.In(loc)
	return time.Date(l.Year(), l.Month(), l.Day(), 0, 0, 0, 0, loc)
}

func pad3(i int) string {
	return fmt.Sprintf("%03d", i)
}
