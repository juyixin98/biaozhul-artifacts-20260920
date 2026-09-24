package scaler

import (
	"math"
	"testing"
)

func testConfig() Config {
	c := Config{}
	c.ApplyDefaults()
	return c
}

func mkSamples(loads map[int64]float64, missing ...int64) []Sample {
	out := make([]Sample, 0, len(loads)+len(missing))
	seen := map[int64]bool{}
	for t, v := range loads {
		out = append(out, Sample{TimeSeconds: t, TotalLoad: v})
		seen[t] = true
	}
	for _, t := range missing {
		if !seen[t] {
			out = append(out, Sample{TimeSeconds: t, Missing: true})
		}
	}
	return SortedSamples(out)
}

// 在冷启动窗口之后执行一步，返回首个非门禁决策。
func decideAfterColdStart(t *testing.T, e *Engine, samples []Sample, at int64) Decision {
	t.Helper()
	d := e.Decide(at, samples)
	if !d.SignalOK {
		t.Fatalf("t=%d 信号应正常，理由: %v", at, d.Reasons)
	}
	return d
}

func TestConfigValidation(t *testing.T) {
	c := testConfig()
	if err := c.Validate(); err != nil {
		t.Fatalf("默认配置应合法: %v", err)
	}
	bad := []func(*Config){
		func(c *Config) { c.MinReplicas = 0 },
		func(c *Config) { c.MaxReplicas = 1 },
		func(c *Config) { c.LoadPerReplica = -1 },
		func(c *Config) { c.UpWindowSeconds = c.DownWindowSeconds + 1 },
		func(c *Config) { c.UpThresholdPercent = 0 },
		func(c *Config) { c.DownThresholdPercent = 100 },
		func(c *Config) { c.MaxUpStep = 0 },
		func(c *Config) { c.FreshnessSeconds = 0 },
	}
	for i, mut := range bad {
		c2 := testConfig()
		mut(&c2)
		if err := c2.Validate(); err == nil {
			t.Errorf("用例 %d 应校验失败", i)
		}
	}
}

func TestColdStartGate(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)
	samples := mkSamples(map[int64]float64{0: 500, 30: 500, 60: 500, 90: 500, 119: 500})
	for _, at := range []int64{0, 60} {
		d := e.Decide(at, samples)
		if d.Action != ActionHold {
			t.Fatalf("t=%d 冷启动窗口内应保持，实际 %s", at, d.Action)
		}
	}
	// 冷启动窗口结束（t=coldStart）后允许决策。
	d := e.Decide(cfg.ColdStartDelaySeconds, mkSamples(map[int64]float64{
		120: 500, 90: 500, 60: 500, 30: 500, 0: 500}))
	if d.Action != ActionUp {
		t.Fatalf("冷启动结束后的高负载应触发扩容，实际 %s（理由 %v）", d.Action, d.Reasons)
	}
}

func TestMissingMetricNeverZero(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)
	// t=120 起负载本会建议缩容，但最新样本缺失。
	samples := mkSamples(map[int64]float64{
		0: 100, 30: 100, 60: 100, 90: 100,
	}, 120)
	d := e.Decide(120, samples)
	if d.Action != ActionHold || d.SignalOK {
		t.Fatalf("最新样本缺失时应保持且 signalOk=false，实际 action=%s signal=%v",
			d.Action, d.SignalOK)
	}
	if d.ReadyAfter != cfg.MinReplicas || d.TargetReplicas != cfg.MinReplicas {
		t.Fatalf("缺失样本不得被当作零负载缩容，ready=%d target=%d",
			d.ReadyAfter, d.TargetReplicas)
	}
}

func TestStaleMetricNeverZero(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)
	// 最新样本停留在 t=60，决策时刻 120，超期 60-45=15s。
	samples := mkSamples(map[int64]float64{0: 100, 30: 100, 60: 100})
	d := e.Decide(120, samples)
	if d.Action != ActionHold || d.SignalOK {
		t.Fatalf("信号过期应保持且 signalOk=false，实际 action=%s signal=%v",
			d.Action, d.SignalOK)
	}
	if d.TargetReplicas != cfg.MinReplicas {
		t.Fatalf("过期指标不得被当作零负载缩容，target=%d", d.TargetReplicas)
	}
}

func TestScaleUpRateLimitedAndColdStarts(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)

	loadAt := func(upTo int64) []Sample {
		m := map[int64]float64{}
		for s := int64(0); s <= upTo; s += 30 {
			m[s] = 900 // 期望 9 副本，需要 +7
		}
		return mkSamples(m)
	}

	// t=120: 首次扩容，最多 +MaxUpStep=2，进入冷启动。
	d := decideAfterColdStart(t, e, loadAt(120), 120)
	if d.Action != ActionUp || d.PendingAfter != 2 {
		t.Fatalf("首次扩容应 +2 冷启动，实际 action=%s pending=%d", d.Action, d.PendingAfter)
	}
	// 冷启动期间任何高负载都不能再决策。
	d = e.Decide(180, loadAt(180))
	if d.Action != ActionHold || d.PendingAfter != 2 {
		t.Fatalf("冷启动期间应保持且仍有 2 个 pending，实际 %s/%d", d.Action, d.PendingAfter)
	}
	// t=240: 副本就绪，同拍可基于窗口再次扩容。
	d = e.Decide(240, loadAt(240))
	if d.ReadyAfter != 4 {
		t.Fatalf("t=240 就绪后应有 4 副本，实际 %d", d.ReadyAfter)
	}
	// 到 t=540 共 3 次扩容（120/300/420 决策拍），ready 应为 8；
	// 因扩容冷却 60s 与冷启动 120s 的节拍，第 4 次扩容需继续推进。
	for _, at := range []int64{300, 360, 420, 480, 540} {
		e.Decide(at, loadAt(at))
	}
	if got := e.Ready(); got != 8 {
		t.Fatalf("限速多拍后就绪副本应为 8，实际 %d", got)
	}
}

func TestScaleDownWindowProtectionAndRateLimit(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)
	// 直接构造“已扩容到 6 副本、扩容已很久”的稳定状态。
	e.ready = 6
	e.hasUp = true
	e.lastUp = 0

	// t<420 负载 700（期望 7）；t>=420 跌落到 100（期望 1，下限 2）。
	samplesUpTo := func(upTo int64) []Sample {
		m := map[int64]float64{}
		for s := int64(0); s <= upTo; s += 30 {
			if s >= 420 {
				m[s] = 100
			} else {
				m[s] = 700
			}
		}
		return mkSamples(m)
	}
	// t=480/600/660：缩容窗口（如 [180,480]）仍包含 700 峰值 → 不缩。
	for _, at := range []int64{480, 600, 660} {
		d := e.Decide(at, samplesUpTo(at))
		if d.Action != ActionHold {
			t.Fatalf("t=%d 缩容稳定窗口未过，应保持，实际 %s（%v）", at, d.Action, d.Reasons)
		}
	}
	// t=720：缩容窗口 [420,720] 全部低负载且完整 → 缩 1（单步限速）。
	d := e.Decide(720, samplesUpTo(720))
	if d.Action != ActionDown || d.ReadyAfter != 5 {
		t.Fatalf("t=720 应缩 1 到 5，实际 action=%s ready=%d（%v）",
			d.Action, d.ReadyAfter, d.Reasons)
	}
	// t=780：缩容冷却 120s 未过 → 保持。
	d = e.Decide(780, samplesUpTo(780))
	if d.Action != ActionHold {
		t.Fatalf("缩容冷却期应保持，实际 %s", d.Action)
	}
	// t=840：冷却过后再缩 1。
	d = e.Decide(840, samplesUpTo(840))
	if d.Action != ActionDown || d.ReadyAfter != 4 {
		t.Fatalf("t=840 应缩 1 到 4，实际 %s/%d", d.Action, d.ReadyAfter)
	}
}

func TestHysteresisBands(t *testing.T) {
	cfg := testConfig() // 上阈值 10%，下阈值 20%
	e := NewEngine(cfg)

	// 当前 2 副本；期望 3/2=1.5 越过 10% → 扩容（但取 +2 限速会 pending 2）。
	samples := mkSamples(map[int64]float64{0: 0, 30: 0, 60: 0, 90: 0, 120: 250})
	d := e.Decide(120, samples)
	if d.Action != ActionUp {
		t.Fatalf("期望 3/2 增幅 50%% 应扩容，实际 %s", d.Action)
	}

	// 新引擎：当前 2，期望 2（负载 150→ceil=2），变化 0 → 迟滞带内保持。
	e2 := NewEngine(cfg)
	s2 := mkSamples(map[int64]float64{
		0: 150, 30: 150, 60: 150, 90: 150, 120: 150})
	d2 := e2.Decide(120, s2)
	if d2.Action != ActionHold {
		t.Fatalf("期望与当前一致时应保持，实际 %s", d2.Action)
	}

	// 当前 ready 手工设为 4，期望 3（降幅 25%）越过 20% 应可进入缩容流程，
	// 但先验证降幅不足的情形：ready=4，长窗口期望 4（负载 350）→ 保持。
	e3 := NewEngine(cfg)
	e3.ready = 4
	e3.hasUp = true
	e3.lastUp = 0 // 扩容稳定窗口早已过
	s3 := mkSamples(loadSeries(0, 900, 30, 350))
	d3 := e3.Decide(900, s3)
	if d3.Action != ActionHold {
		t.Fatalf("期望 4/当前 4 应保持，实际 %s", d3.Action)
	}
}

func TestMinMaxClamp(t *testing.T) {
	cfg := testConfig()
	cfg.MaxReplicas = 5

	e := NewEngine(cfg)
	// 持续 900 负载（期望 9），多拍扩到上限 5 后不得超过。
	for _, at := range []int64{0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 660, 720} {
		m := map[int64]float64{}
		for s := int64(0); s <= at; s += 30 {
			m[s] = 900
		}
		e.Decide(at, mkSamples(m))
	}
	if got := e.Ready() + e.Pending(); got > 5 {
		t.Fatalf("总副本不得超过 maxReplicas=5，实际 %d", got)
	}
}

func TestScaleUpStabilizationBlocksScaleDown(t *testing.T) {
	cfg := testConfig()
	cfg.ScaleUpStabilizationSeconds = 300
	cfg.DownWindowSeconds = 120
	cfg.DownCooldownSeconds = 0
	e := NewEngine(cfg)
	e.ready = 4

	// 最近一次“扩容”发生在 t=480；t=600 时长窗口低负载且完整，
	// 但距上次扩容仅 120s < 300s 稳定期 → 拒绝缩容。
	e.hasUp = true
	e.lastUp = 480
	m := map[int64]float64{}
	for s := int64(480); s <= 600; s += 30 {
		m[s] = 50
	}
	// 窗口 [480,600] 全部低负载。
	d := e.Decide(600, mkSamples(m))
	if d.Action != ActionHold {
		t.Fatalf("扩容稳定窗口内应禁止缩容，实际 %s（%v）", d.Action, d.Reasons)
	}
	found := false
	for _, r := range d.Reasons {
		if contains(r, "稳定窗口") {
			found = true
		}
	}
	if !found {
		t.Fatalf("理由中应说明扩容稳定窗口拦截，实际 %v", d.Reasons)
	}
}

func TestWindowCompleteGapsAndMissing(t *testing.T) {
	from, to := int64(300), int64(600)
	good := mkSamples(loadSeries(0, 600, 30, 100))
	if !windowComplete(good, from, to, 45, 45) {
		t.Fatal("30s 间隔、无缺失的窗口应完整")
	}
	// 窗口内出现显式缺失 → 不完整。
	var withMissing []Sample
	for _, s := range good {
		if s.TimeSeconds == 450 {
			withMissing = append(withMissing, Sample{TimeSeconds: 450, Missing: true})
		} else {
			withMissing = append(withMissing, s)
		}
	}
	if windowComplete(withMissing, from, to, 45, 45) {
		t.Fatal("窗口内含缺失样本应判不完整")
	}
	// 窗口内出现 90s 的采样空洞 → 不完整。
	gapped := mkSamples(loadSeries(0, 600, 30, 100))
	var filtered []Sample
	for _, s := range gapped {
		if s.TimeSeconds != 420 && s.TimeSeconds != 450 {
			filtered = append(filtered, s)
		}
	}
	if windowComplete(filtered, from, to, 45, 45) {
		t.Fatal("窗口内 90s 采样空洞应判不完整")
	}
	// 最后样本过旧 → 右边界未覆盖。
	stale := mkSamples(loadSeries(0, 540, 30, 100))
	if windowComplete(stale, from, to, 45, 45) {
		t.Fatal("决策时刻无新鲜样本应判不完整")
	}
}

func TestUsedSamplesRecorded(t *testing.T) {
	cfg := testConfig()
	e := NewEngine(cfg)
	samples := mkSamples(map[int64]float64{
		30: 100, 60: 200, 270: 300, 300: 300,
	})
	d := e.Decide(300, samples)
	if len(d.UsedSamples) != 4 {
		t.Fatalf("应记录全部 4 个可见样本，实际 %d", len(d.UsedSamples))
	}
	byTime := map[int64]UsedSample{}
	for _, us := range d.UsedSamples {
		byTime[us.TimeSeconds] = us
	}
	// t=30：只在缩容窗口 [1,300] 内，不在扩容窗口 [241,300]；年龄 270>45 不新鲜。
	s0 := byTime[30]
	if s0.InUpWindow || !s0.InDownWindow || s0.Fresh {
		t.Fatalf("t=30 样本窗口归属/新鲜度错误: %+v", s0)
	}
	// t=300：两个窗口都在、最新、新鲜。
	s300 := byTime[300]
	if !s300.InUpWindow || !s300.InDownWindow || !s300.Fresh {
		t.Fatalf("t=300 样本窗口归属/新鲜度错误: %+v", s300)
	}
	// t=60：在缩容窗口、不在扩容窗口。
	s60 := byTime[60]
	if s60.InUpWindow || !s60.InDownWindow {
		t.Fatalf("t=60 样本窗口归属错误: %+v", s60)
	}
	// 未来样本不可见。
	future := append(SortedSamples(samples), Sample{TimeSeconds: 360, TotalLoad: 9999})
	d2 := e.Decide(300, future)
	for _, us := range d2.UsedSamples {
		if us.TimeSeconds > 300 {
			t.Fatal("决策不得使用未来样本")
		}
	}
}

func TestValidateSamples(t *testing.T) {
	if err := ValidateSamples(mkSamples(map[int64]float64{0: 1, 30: 2})); err != nil {
		t.Fatalf("合法样本被拒: %v", err)
	}
	if err := ValidateSamples([]Sample{
		{TimeSeconds: 30, TotalLoad: 1},
		{TimeSeconds: 30, TotalLoad: 2},
	}); err == nil {
		t.Fatal("重复时间戳应被拒绝")
	}
	if err := ValidateSamples([]Sample{
		{TimeSeconds: -1, Missing: true},
	}); err == nil {
		t.Fatal("负时间戳应被拒绝")
	}
}

func TestCeilDiv(t *testing.T) {
	cases := []struct {
		load, per float64
		want      int
	}{
		{0, 100, 0},
		{1, 100, 1},
		{100, 100, 1},
		{101, 100, 2},
		{300, 100, 3},
	}
	for _, c := range cases {
		if got := ceilDiv(c.load, c.per); got != c.want {
			t.Errorf("ceilDiv(%v,%v)=%d, want %d", c.load, c.per, got, c.want)
		}
	}
	if math.IsNaN(float64(ceilDiv(0, 100))) {
		t.Fatal("零负载期望应为 0 而非 NaN")
	}
}

func loadSeries(from, to, step int64, load float64) map[int64]float64 {
	m := map[int64]float64{}
	for t := from; t <= to; t += step {
		m[t] = load
	}
	return m
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
