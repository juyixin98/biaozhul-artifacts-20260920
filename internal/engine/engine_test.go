package engine

import (
	"testing"
	"time"
)

// fakeClock 测试时钟：时间完全由测试控制，Advance 也接受显式时刻，
// 因此全部测试零真实睡眠。
type fakeClock struct{ t time.Time }

func (f *fakeClock) Now() time.Time { return f.t }

func utc(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func newEngine(at time.Time) (*Engine, *fakeClock) {
	fc := &fakeClock{t: at}
	return New(fc, time.Second), fc
}

func mustAdd(t *testing.T, e *Engine, id, mins, hrs, wds, tz string, limit int) *Trigger {
	t.Helper()
	tr, err := e.AddTrigger(id, mins, hrs, wds, tz, limit)
	if err != nil {
		t.Fatalf("AddTrigger(%s) 失败: %v", id, err)
	}
	return tr
}

func TestNormalTickFires(t *testing.T) {
	eng, _ := newEngine(utc("2026-09-24T10:00:30Z")) // 非整分钟启动，避开边界
	tr := mustAdd(t, eng, "a", "*", "*", "*", "UTC", 100)

	// 逐秒推进 3 个分钟边界，应触发 3 次且都不是补触发
	base := utc("2026-09-24T10:00:30Z")
	for i := 1; i <= 200; i++ {
		eng.Advance(base.Add(time.Duration(i) * time.Second))
	}
	evs := eng.Events("a", 0)
	if len(evs) != 3 {
		t.Fatalf("200 秒推进触发 %d 次，期望 3 次（10:01/10:02/10:03）", len(evs))
	}
	for _, ev := range evs {
		if ev.CaughtUp {
			t.Errorf("正常 tick 不应标记为补触发: %+v", ev)
		}
	}
	if tr.TotalFired != 3 {
		t.Errorf("TotalFired=%d，期望 3", tr.TotalFired)
	}
}

// 停机 1 小时（逐分钟表达式共 60 次错过），补触发上限 5：
// 只补最新 5 次，其余 55 次计入 MissedDropped。
func TestDowntimeCatchUpCap(t *testing.T) {
	t0 := utc("2026-09-24T10:00:00Z")
	eng, _ := newEngine(t0)
	tr := mustAdd(t, eng, "a", "*", "*", "*", "UTC", 5)

	eng.Advance(t0.Add(time.Hour)) // 模拟停机 1 小时后恢复

	evs := eng.Events("a", 0)
	if len(evs) != 5 {
		t.Fatalf("补触发 %d 次，期望上限 5 次", len(evs))
	}
	for _, ev := range evs {
		if !ev.CaughtUp {
			t.Errorf("停机恢复后的触发应标记为补触发: %+v", ev)
		}
	}
	// 保留最新的 5 次：10:55–10:59
	if got := evs[0].WallClock; got != "2026-09-24T10:55" {
		t.Errorf("最早的补触发 = %s，期望 2026-09-24T10:55（丢弃最旧的）", got)
	}
	if got := evs[4].WallClock; got != "2026-09-24T10:59" {
		t.Errorf("最新的补触发 = %s，期望 2026-09-24T10:59", got)
	}
	if tr.MissedDropped != 55 {
		t.Errorf("MissedDropped=%d，期望 55（60 次错过 - 5 次补触发）", tr.MissedDropped)
	}
	if tr.TotalFired != 5 {
		t.Errorf("TotalFired=%d，期望 5", tr.TotalFired)
	}
}

// 上限为 0：完全不补触发，但错过次数仍准确记账；
// 恢复正常 tick 后，按时到期的触发不受影响。
func TestCatchUpLimitZero(t *testing.T) {
	t0 := utc("2026-09-24T10:00:00Z")
	eng, _ := newEngine(t0)
	tr := mustAdd(t, eng, "a", "*", "*", "*", "UTC", 0)

	eng.Advance(t0.Add(time.Hour))
	if evs := eng.Events("a", 0); len(evs) != 0 {
		t.Fatalf("上限 0 不应补触发，实际 %d 次", len(evs))
	}
	if tr.MissedDropped != 60 {
		t.Errorf("MissedDropped=%d，期望 60", tr.MissedDropped)
	}

	// 恢复正常逐秒推进：下一个分钟边界（11:00）按时触发，不受上限 0 影响
	eng.Advance(t0.Add(time.Hour).Add(time.Second))
	evs := eng.Events("a", 0)
	if len(evs) != 1 {
		t.Fatalf("恢复后应按时触发 1 次，实际 %d 次", len(evs))
	}
	if evs[0].CaughtUp {
		t.Error("恢复后的按时触发不应标记为补触发")
	}
	if evs[0].WallClock != "2026-09-24T11:00" {
		t.Errorf("恢复后触发墙钟 = %s，期望 2026-09-24T11:00", evs[0].WallClock)
	}
}

// 时钟回拨后重叠窗口重放：逻辑触发 ID 去重，同一墙钟时刻不重复执行。
func TestDedupOnClockRewind(t *testing.T) {
	t0 := utc("2026-09-24T10:00:00Z")
	eng, _ := newEngine(t0)
	tr := mustAdd(t, eng, "a", "*", "*", "*", "UTC", 100)

	eng.Advance(t0.Add(time.Hour)) // 触发 10:00–10:59 共 60 次
	if n := len(eng.Events("a", 0)); n != 60 {
		t.Fatalf("首次推进触发 %d 次，期望 60", n)
	}

	// 时钟回拨 30 分钟，再推进到相同时刻：重叠的 30 分钟被重放
	eng.Advance(t0.Add(30 * time.Minute)) // 回拨：checkpoint 重置
	eng.Advance(t0.Add(time.Hour))        // 重放 [10:30, 11:00)

	evs := eng.Events("a", 0)
	if len(evs) != 60 {
		t.Fatalf("去重后事件 %d 条，期望仍是 60 条", len(evs))
	}
	if tr.DedupSkipped != 30 {
		t.Errorf("DedupSkipped=%d，期望 30（10:30–10:59 重放被去重）", tr.DedupSkipped)
	}
	if tr.TotalFired != 60 {
		t.Errorf("TotalFired=%d，期望 60", tr.TotalFired)
	}
	// 逻辑 ID 唯一性
	seen := map[string]bool{}
	for _, ev := range evs {
		if seen[ev.LogicalID] {
			t.Errorf("逻辑触发 ID 重复: %s", ev.LogicalID)
		}
		seen[ev.LogicalID] = true
	}
}

// 引擎层面验证秋季切换：2026-11-01 America/New_York 01:30 只触发一次，
// 且对应较早的那次出现（05:30 UTC，EDT 侧）。
func TestEngineFallBackOnce(t *testing.T) {
	eng, _ := newEngine(utc("2026-10-31T00:00:00Z"))
	mustAdd(t, eng, "dst", "30", "1", "*", "America/New_York", 100)

	eng.Advance(utc("2026-11-03T00:00:00Z"))
	evs := eng.Events("dst", 0)
	if len(evs) != 3 {
		t.Fatalf("秋季窗口触发 %d 次，期望 3 次（10-31/11-01/11-02）", len(evs))
	}
	var nov1 *Event
	for i := range evs {
		if evs[i].WallClock == "2026-11-01T01:30" {
			nov1 = &evs[i]
		}
	}
	if nov1 == nil {
		t.Fatal("缺少 2026-11-01T01:30 事件")
	}
	if got := nov1.FiredAtUTC.Format(time.RFC3339); got != "2026-11-01T05:30:00Z" {
		t.Errorf("11-01 01:30 触发瞬间 = %s，期望较早一次 2026-11-01T05:30:00Z", got)
	}
	if nov1.LogicalID != "dst@2026-11-01T01:30" {
		t.Errorf("逻辑触发 ID = %s，期望 dst@2026-11-01T01:30", nov1.LogicalID)
	}
}

// 引擎层面验证春季切换：2026-03-08 America/New_York 02:30 缺失，跳过。
func TestEngineSpringForwardSkipped(t *testing.T) {
	eng, _ := newEngine(utc("2026-03-07T00:00:00Z"))
	mustAdd(t, eng, "dst", "30", "2", "*", "America/New_York", 100)

	eng.Advance(utc("2026-03-10T00:00:00Z"))
	evs := eng.Events("dst", 0)
	if len(evs) != 2 {
		t.Fatalf("春季窗口触发 %d 次，期望 2 次（03-07/03-09）", len(evs))
	}
	for _, ev := range evs {
		if ev.WallClock == "2026-03-08T02:30" {
			t.Error("缺失时刻 2026-03-08T02:30 不应触发")
		}
	}
	if evs[0].WallClock != "2026-03-07T02:30" || evs[1].WallClock != "2026-03-09T02:30" {
		t.Errorf("触发墙钟 = %s, %s", evs[0].WallClock, evs[1].WallClock)
	}
}

// 触发器创建之前的时刻不补触发。
func TestNoCatchUpBeforeCreation(t *testing.T) {
	t0 := utc("2026-09-24T10:00:00Z")
	eng, fc := newEngine(t0)
	fc.t = utc("2026-09-24T10:30:00Z") // 30 分钟后才创建触发器
	mustAdd(t, eng, "late", "*", "*", "*", "UTC", 100)

	eng.Advance(utc("2026-09-24T11:00:00Z"))
	evs := eng.Events("late", 0)
	// 只应补创建之后的时刻：10:30:00 创建，[10:30, 11:00) 共 30 次
	if len(evs) != 30 {
		t.Fatalf("创建后补触发 %d 次，期望 30 次（10:30–10:59）", len(evs))
	}
	for _, ev := range evs {
		if ev.WallClock < "2026-09-24T10:30" {
			t.Errorf("创建之前的时刻 %s 不应补触发", ev.WallClock)
		}
	}
}

func TestAddTriggerValidation(t *testing.T) {
	eng, _ := newEngine(utc("2026-09-24T10:00:00Z"))
	if _, err := eng.AddTrigger("x", "bad", "*", "*", "UTC", 1); err == nil {
		t.Error("非法表达式应报错")
	}
	if _, err := eng.AddTrigger("x", "*", "*", "*", "Mars/Olympus", 1); err == nil {
		t.Error("未知时区应报错")
	}
	if _, err := eng.AddTrigger("x", "*", "*", "*", "UTC", -1); err == nil {
		t.Error("负的补触发上限应报错")
	}
	mustAdd(t, eng, "x", "*", "*", "*", "UTC", 1)
	if _, err := eng.AddTrigger("x", "*", "*", "*", "UTC", 1); err == nil {
		t.Error("重复 ID 应报错")
	}
}
