package scaler_test

import (
	"strings"
	"testing"

	"offline-scaler/internal/scaler"
)

// findDecision 返回首个满足谓词的决策。
func findDecision(t *testing.T, ds []scaler.Decision, pred func(scaler.Decision) bool) (scaler.Decision, bool) {
	t.Helper()
	for _, d := range ds {
		if pred(d) {
			return d, true
		}
	}
	return scaler.Decision{}, false
}

func reasonText(d scaler.Decision) string { return strings.Join(d.Reasons, " | ") }

// TestScenarioSpike 验收场景一：突发尖峰。
func TestScenarioSpike(t *testing.T) {
	res, err := scaler.Replay(scaler.SpikeScenario().Request)
	if err != nil {
		t.Fatal(err)
	}
	ds := res.Decisions

	// 1) 初始冷启动窗口（t<120）内即使有 900 尖峰也必须保持。
	for _, d := range ds {
		if d.TimeSeconds < 120 && d.Action != scaler.ActionHold {
			t.Fatalf("t=%d 冷启动窗口内不应动作，实际 %s", d.TimeSeconds, d.Action)
		}
	}

	// 2) 扩容限速：高平台期望 9 副本，单次扩容最多 +2，且每次扩容后冷启动 120s。
	var maxStep int
	var sawColdStartBlock bool
	for _, d := range ds {
		if d.Action == scaler.ActionUp {
			if step := d.PendingAfter; step > 2 {
				t.Fatalf("t=%d 单步扩容 %d 超过限速 2", d.TimeSeconds, step)
			} else if step > maxStep {
				maxStep = step
			}
		}
		if strings.Contains(reasonText(d), "仍在冷启动中") {
			sawColdStartBlock = true
		}
	}
	if res.Summary.ScaleUps < 3 {
		t.Fatalf("高平台应触发至少 3 次扩容，实际 %d", res.Summary.ScaleUps)
	}
	if maxStep > 2 {
		t.Fatalf("扩容单步限速失效: %d", maxStep)
	}
	if !sawColdStartBlock {
		t.Fatal("应出现“新副本冷启动中不做决策”的节拍")
	}

	// 3) 回落后必须等 300s 缩容稳定窗口全部为低负载才缩：首次缩容时刻 >= 720
	//    （负载 420 回落，窗口 [420,720] 在 t=720 首次完整）。
	firstDown, ok := findDecision(t, ds, func(d scaler.Decision) bool {
		return d.Action == scaler.ActionDown
	})
	if !ok {
		t.Fatal("应至少发生一次缩容")
	}
	if firstDown.TimeSeconds < 720 {
		t.Fatalf("缩容稳定窗口保护失效：t=%d 就开始缩容", firstDown.TimeSeconds)
	}

	// 4) 缩容单步限速 -1 与冷却 120s：任意两次缩容至少间隔 120s。
	var lastDownAt int64 = -1000
	for _, d := range ds {
		if d.Action == scaler.ActionDown {
			if d.ReadyBefore-d.ReadyAfter != 1 {
				t.Fatalf("t=%d 单步缩容数应为 1，实际 %d",
					d.TimeSeconds, d.ReadyBefore-d.ReadyAfter)
			}
			if d.TimeSeconds-lastDownAt < 120 {
				t.Fatalf("t=%d 距上次缩容不足 120s 冷却", d.TimeSeconds)
			}
			lastDownAt = d.TimeSeconds
		}
	}

	// 5) 最终回到 minReplicas=2，全程不超过 maxReplicas。
	if res.Summary.FinalReady != 2 {
		t.Fatalf("最终就绪副本应为 2，实际 %d", res.Summary.FinalReady)
	}
	if res.Summary.MaxTotalReplicas > 10 {
		t.Fatalf("副本数超过上限: %d", res.Summary.MaxTotalReplicas)
	}
}

// TestScenarioRamp 验收场景二：持续增长后跌落。
func TestScenarioRamp(t *testing.T) {
	res, err := scaler.Replay(scaler.RampScenario().Request)
	if err != nil {
		t.Fatal(err)
	}
	ds := res.Decisions

	// 1) 增长阶段逐拍限速扩容，任何一拍新增 <= 2。
	for _, d := range ds {
		if d.Action == scaler.ActionUp && d.PendingAfter > 2 {
			t.Fatalf("t=%d 单步扩容 %d 超过限速", d.TimeSeconds, d.PendingAfter)
		}
	}

	// 2) 上限钳制：峰值负载期望 19 副本，实际总副本被钳在 10。
	if res.Summary.MaxTotalReplicas != 10 {
		t.Fatalf("上限钳制失效：峰值总副本=%d，应为 10", res.Summary.MaxTotalReplicas)
	}
	d720, ok := findDecision(t, ds, func(d scaler.Decision) bool { return d.TimeSeconds == 720 })
	if !ok {
		t.Fatal("缺少 t=720 决策")
	}
	if !d720.Clamped || d720.ProposedTarget != 10 {
		t.Fatalf("t=720 期望应被上限钳制为 10，clamped=%v proposed=%d",
			d720.Clamped, d720.ProposedTarget)
	}

	// 3) 跌落（t=1200）后 300s 内不缩容。
	for _, d := range ds {
		if d.TimeSeconds > 1200 && d.TimeSeconds < 1500 && d.Action == scaler.ActionDown {
			t.Fatalf("t=%d 处于缩容稳定窗口内却执行了缩容", d.TimeSeconds)
		}
	}
	firstDown, ok := findDecision(t, ds, func(d scaler.Decision) bool {
		return d.Action == scaler.ActionDown
	})
	if !ok || firstDown.TimeSeconds != 1500 {
		got := int64(-1)
		if ok {
			got = firstDown.TimeSeconds
		}
		t.Fatalf("首次缩容应发生在 t=1500，实际 %d", got)
	}

	// 4) 缩容冷却 120s + 单步 -1；最终回落到下限 2 并被迟滞带守住。
	var lastDownAt int64 = -1000
	downs := 0
	for _, d := range ds {
		if d.Action == scaler.ActionDown {
			if d.TimeSeconds-lastDownAt < 120 {
				t.Fatalf("t=%d 缩容冷却失效", d.TimeSeconds)
			}
			if d.ReadyBefore-d.ReadyAfter != 1 {
				t.Fatalf("t=%d 单步缩容不是 -1", d.TimeSeconds)
			}
			lastDownAt = d.TimeSeconds
			downs++
		}
	}
	if downs < 8 {
		t.Fatalf("从 10 缩到 2 应发生 8 次缩容，实际 %d", downs)
	}
	if res.Summary.FinalReady != 2 {
		t.Fatalf("最终应回到 minReplicas=2，实际 %d", res.Summary.FinalReady)
	}
}

// TestScenarioOscillation 验收场景三：周期振荡 + 采集中断。
func TestScenarioOscillation(t *testing.T) {
	res, err := scaler.Replay(scaler.OscillationScenario().Request)
	if err != nil {
		t.Fatal(err)
	}
	ds := res.Decisions

	// 1) 波峰阶段发生扩容，且任何一拍 +<=2。
	if res.Summary.ScaleUps == 0 {
		t.Fatal("波峰应触发扩容")
	}
	for _, d := range ds {
		if d.Action == scaler.ActionUp && d.PendingAfter > 2 {
			t.Fatalf("t=%d 单步扩容超过限速", d.TimeSeconds)
		}
	}

	// 2) t=600/660 最新样本缺失：保持、signalOk=false、副本数不变（不当零负载）。
	for _, want := range []int64{600, 660} {
		d, ok := findDecision(t, ds, func(d scaler.Decision) bool { return d.TimeSeconds == want })
		if !ok {
			t.Fatalf("缺少 t=%d 决策", want)
		}
		if d.Action != scaler.ActionHold || d.SignalOK {
			t.Fatalf("t=%d 缺失指标应保持且 signalOk=false，实际 %s/%v",
				want, d.Action, d.SignalOK)
		}
		if d.TargetReplicas != 7 {
			t.Fatalf("t=%d 缺失指标被当作零负载了：target=%d", want, d.TargetReplicas)
		}
		if !strings.Contains(reasonText(d), "缺失不当零负载") {
			t.Fatalf("t=%d 理由未说明缺失策略: %v", want, d.Reasons)
		}
	}

	// 3) 恢复后、缺口仍落在缩容窗口内期间（720~960），窗口不完整 → 拒绝缩容。
	for _, d := range ds {
		if d.TimeSeconds >= 720 && d.TimeSeconds <= 960 {
			if d.Action == scaler.ActionDown {
				t.Fatalf("t=%d 缩容窗口含缺口却执行了缩容", d.TimeSeconds)
			}
		}
	}
	d720, ok := findDecision(t, ds, func(d scaler.Decision) bool { return d.TimeSeconds == 720 })
	if ok && d720.DownWindowComplete {
		t.Fatal("t=720 缩容窗口包含缺失段，应判不完整")
	}

	// 4) 迟滞吸振：副本数在大部分振荡周期保持不动（保持占比应很高）。
	if res.Summary.Holds <= res.Summary.Ticks*3/4 {
		t.Fatalf("迟滞未能吸收振荡：保持 %d/%d 拍", res.Summary.Holds, res.Summary.Ticks)
	}

	// 5) 决策记录必须带所用样本与理由。
	for _, d := range ds {
		if len(d.Reasons) == 0 {
			t.Fatalf("t=%d 决策缺少理由", d.TimeSeconds)
		}
		if len(d.UsedSamples) == 0 && d.TimeSeconds >= 30 {
			t.Fatalf("t=%d 决策未记录所用样本", d.TimeSeconds)
		}
	}
}

// TestReplayRejectsInvalidInputs 回放入口的输入校验。
func TestReplayRejectsInvalidInputs(t *testing.T) {
	base := scaler.SpikeScenario().Request

	bad := base
	bad.Config.MaxReplicas = 1 // minReplicas 默认 2，非法
	if _, err := scaler.Replay(bad); err == nil {
		t.Fatal("maxReplicas<minReplicas 应被拒绝")
	}

	bad = base
	bad.Samples = append(bad.Samples, scaler.Sample{TimeSeconds: 30, TotalLoad: 1}) // 与已有 t=30 冲突
	if _, err := scaler.Replay(bad); err == nil {
		t.Fatal("重复时间戳样本应被拒绝")
	}

	bad = base
	bad.DecisionTimes = nil
	bad.DecisionIntervalSec = 0
	if _, err := scaler.Replay(bad); err == nil {
		t.Fatal("缺少决策时刻来源应被拒绝")
	}
}
