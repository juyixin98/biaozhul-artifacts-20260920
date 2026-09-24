// Package scaler 实现离线（回放式）扩缩容决策引擎。
//
// 引擎在离散的时间步上推进，每个时间步消费截至该时刻的指标样本，
// 依次应用：信号存活检查 → 冷启动门禁 → 期望副本计算（稳定窗口）→
// 迟滞带 → 扩容/缩容冷却 → 单步限速 → 上下限钳制。
//
// 设计约定：
//   - 指标缺失（missing=true）绝不按零负载处理；
//   - 最近一个样本超过 FreshnessSeconds 即视为信号失效，本拍保持副本；
//   - 缩容需要整个缩容窗口被“连续、新鲜、非缺失”的样本覆盖（缩容保护）；
//   - 扩容采用窗口内最保守（负载最高）的样本，宁可多扩、不误缩。
package scaler

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

// Action 为单个时间步的决策动作。
type Action string

const (
	ActionUp   Action = "scale_up"   // 扩容
	ActionDown Action = "scale_down" // 缩容
	ActionHold Action = "hold"       // 保持
)

// Sample 是一个时间点上的负载指标样本。
//
// 口径为“服务总负载”（例如总 QPS），与 Config.LoadPerReplica 相除得到副本数。
// Missing=true 表示该时刻采集到了样本点，但指标缺失（上报方/监控系统
// 明确给不出值）。缺失样本绝不按负载 0 处理。
type Sample struct {
	TimeSeconds int64   `json:"timeSeconds"`
	TotalLoad   float64 `json:"totalLoad"`
	Missing     bool    `json:"missing,omitempty"`
}

// Config 是扩缩容策略配置。零值结构在调用 ApplyDefaults 后即可使用。
type Config struct {
	MinReplicas int `json:"minReplicas"`
	MaxReplicas int `json:"maxReplicas"`

	// LoadPerReplica 每个副本可承载的负载量（与样本口径一致）。
	LoadPerReplica float64 `json:"loadPerReplica"`

	// UpWindowSeconds 扩容观测窗口（短窗口，快速响应）。
	UpWindowSeconds int64 `json:"upWindowSeconds"`
	// DownWindowSeconds 缩容稳定窗口（长窗口，要求持续低负载）。
	DownWindowSeconds int64 `json:"downWindowSeconds"`

	// UpThresholdPercent 扩容迟滞阈值：raw/current >= 1+阈值/100 才扩容。
	UpThresholdPercent float64 `json:"upThresholdPercent"`
	// DownThresholdPercent 缩容迟滞阈值：raw/current <= 1-阈值/100 才缩容。
	DownThresholdPercent float64 `json:"downThresholdPercent"`

	// MaxUpStep 单个时间步最多新增的副本数（限速）。
	MaxUpStep int `json:"maxUpStep"`
	// MaxDownStep 单个时间步最多减少的副本数（缩容限速）。
	MaxDownStep int `json:"maxDownStep"`

	// UpCooldownSeconds 上一次（被接受的）扩容后，再次允许扩容的冷却时间。
	UpCooldownSeconds int64 `json:"upCooldownSeconds"`
	// DownCooldownSeconds 上一次缩容后再次允许缩容的冷却时间。
	DownCooldownSeconds int64 `json:"downCooldownSeconds"`
	// ScaleUpStabilizationSeconds 扩容后禁止缩容的稳定时间（防止刚扩完就缩）。
	ScaleUpStabilizationSeconds int64 `json:"scaleUpStabilizationSeconds"`

	// ColdStartDelaySeconds 冷启动/新副本就绪延迟：
	// 初始冷启动阶段，以及每次扩容后的该段时间内，不做任何扩缩容决策。
	ColdStartDelaySeconds int64 `json:"coldStartDelaySeconds"`

	// FreshnessSeconds 指标存活上限：最近样本比决策时刻旧超过该值即视为信号失效。
	FreshnessSeconds int64 `json:"freshnessSeconds"`
	// MaxSampleGapSeconds 缩容窗口允许的最大采样间隔（超过则窗口不完整）。
	MaxSampleGapSeconds int64 `json:"maxSampleGapSeconds"`
}

// ApplyDefaults 用默认值填充未设置（零值）的字段。
func (c *Config) ApplyDefaults() {
	if c.MinReplicas == 0 {
		c.MinReplicas = 2
	}
	if c.MaxReplicas == 0 {
		c.MaxReplicas = 10
	}
	if c.LoadPerReplica == 0 {
		c.LoadPerReplica = 100
	}
	if c.UpWindowSeconds == 0 {
		c.UpWindowSeconds = 60
	}
	if c.DownWindowSeconds == 0 {
		c.DownWindowSeconds = 300
	}
	if c.UpThresholdPercent == 0 {
		c.UpThresholdPercent = 10
	}
	if c.DownThresholdPercent == 0 {
		c.DownThresholdPercent = 20
	}
	if c.MaxUpStep == 0 {
		c.MaxUpStep = 2
	}
	if c.MaxDownStep == 0 {
		c.MaxDownStep = 1
	}
	if c.UpCooldownSeconds == 0 {
		c.UpCooldownSeconds = 60
	}
	if c.DownCooldownSeconds == 0 {
		c.DownCooldownSeconds = 120
	}
	if c.ScaleUpStabilizationSeconds == 0 {
		c.ScaleUpStabilizationSeconds = 180
	}
	if c.ColdStartDelaySeconds == 0 {
		c.ColdStartDelaySeconds = 120
	}
	if c.FreshnessSeconds == 0 {
		c.FreshnessSeconds = 45
	}
	if c.MaxSampleGapSeconds == 0 {
		c.MaxSampleGapSeconds = 45
	}
}

// Validate 校验配置合法性。
func (c Config) Validate() error {
	switch {
	case c.MinReplicas < 1:
		return errors.New("minReplicas 必须 >= 1")
	case c.MaxReplicas < c.MinReplicas:
		return errors.New("maxReplicas 不能小于 minReplicas")
	case c.LoadPerReplica <= 0:
		return errors.New("loadPerReplica 必须 > 0")
	case c.UpWindowSeconds <= 0:
		return errors.New("upWindowSeconds 必须 > 0")
	case c.DownWindowSeconds <= 0:
		return errors.New("downWindowSeconds 必须 > 0")
	case c.UpWindowSeconds > c.DownWindowSeconds:
		return errors.New("upWindowSeconds 不能大于 downWindowSeconds")
	case c.UpThresholdPercent <= 0 || c.UpThresholdPercent >= 100:
		return errors.New("upThresholdPercent 必须在 (0,100) 之间")
	case c.DownThresholdPercent <= 0 || c.DownThresholdPercent >= 100:
		return errors.New("downThresholdPercent 必须在 (0,100) 之间")
	case c.MaxUpStep < 1:
		return errors.New("maxUpStep 必须 >= 1")
	case c.MaxDownStep < 1:
		return errors.New("maxDownStep 必须 >= 1")
	case c.UpCooldownSeconds < 0:
		return errors.New("upCooldownSeconds 不能为负")
	case c.DownCooldownSeconds < 0:
		return errors.New("downCooldownSeconds 不能为负")
	case c.ScaleUpStabilizationSeconds < 0:
		return errors.New("scaleUpStabilizationSeconds 不能为负")
	case c.ColdStartDelaySeconds < 0:
		return errors.New("coldStartDelaySeconds 不能为负")
	case c.FreshnessSeconds <= 0:
		return errors.New("freshnessSeconds 必须 > 0")
	case c.MaxSampleGapSeconds <= 0:
		return errors.New("maxSampleGapSeconds 必须 > 0")
	}
	return nil
}

// UsedSample 记录一个样本在某次决策中的角色。
type UsedSample struct {
	Sample
	InUpWindow   bool `json:"inUpWindow"`
	InDownWindow bool `json:"inDownWindow"`
	Fresh        bool `json:"fresh"`
}

// Decision 是一个时间步的完整决策记录（含依据，用于审计与验收）。
type Decision struct {
	TimeSeconds int64  `json:"timeSeconds"`
	Action      Action `json:"action"`

	// ReadyBefore / ReadyAfter 为该步推进前后的就绪副本数。
	ReadyBefore int `json:"readyBefore"`
	ReadyAfter  int `json:"readyAfter"`
	// Pending 为该步结束时尚在冷启动、未就绪的副本数。
	PendingAfter int `json:"pendingAfter"`

	// RawUpDesired / RawDownDesired 为窗口样本直接给出的期望副本（未钳制）。
	RawUpDesired   int `json:"rawUpDesired"`
	RawDownDesired int `json:"rawDownDesired"`
	// Clamped 表示期望副本曾被 min/max 上下限钳制。
	Clamped bool `json:"clamped,omitempty"`

	// SignalOK 为 false 表示最近样本缺失/过期，本拍不允许任何决策。
	SignalOK bool `json:"signalOk"`
	// DownWindowComplete 表示缩容窗口被连续新鲜样本覆盖（缩容前置条件）。
	DownWindowComplete bool `json:"downWindowComplete"`

	// UpWindowMaxLoad / DownWindowMaxLoad 为两个窗口内的最高总负载（依据）。
	UpWindowMaxLoad   float64 `json:"upWindowMaxLoad,omitempty"`
	DownWindowMaxLoad float64 `json:"downWindowMaxLoad,omitempty"`
	LatestLoad        float64 `json:"latestLoad,omitempty"`

	// ProposedTarget 为迟滞/限速处理前、钳制后的期望副本；无建议时为 -1。
	ProposedTarget int `json:"proposedTarget"`
	// TargetReplicas 为本步最终生效的目标副本（ReadyAfter+PendingAfter）。
	TargetReplicas int `json:"targetReplicas"`

	// Reasons 按检查顺序记录本步结论的人类可读理由。
	Reasons []string `json:"reasons"`
	// UsedSamples 为决策时刻可见（t <= now）的全部样本及其窗口归属。
	UsedSamples []UsedSample `json:"usedSamples"`
}

// Engine 是有状态的离线扩缩容控制器。
type Engine struct {
	cfg Config

	ready    int   // 已就绪副本
	pending  int   // 冷启动中的副本
	readyAt  int64 // pending 副本预计就绪时刻；0 表示无 pending
	lastUp   int64 // 最近一次被接受的扩容时刻
	hasUp    bool  // 是否发生过扩容
	lastDown int64 // 最近一次被接受的缩容时刻
	hasDown  bool  // 是否发生过缩容
}

// NewEngine 创建控制器。MinReplicas 个副本视为初始已就绪。
func NewEngine(cfg Config) *Engine {
	return &Engine{
		cfg:   cfg,
		ready: cfg.MinReplicas,
	}
}

// Ready 返回当前就绪副本数。
func (e *Engine) Ready() int { return e.ready }

// Pending 返回冷启动中副本数。
func (e *Engine) Pending() int { return e.pending }

// Decide 推进到 nowSeconds，结合样本做出本步决策。
// samples 必须按时间升序、时间戳唯一（由 ValidateSamples 保证）。
func (e *Engine) Decide(nowSeconds int64, samples []Sample) Decision {
	d := Decision{
		TimeSeconds:    nowSeconds,
		ReadyBefore:    e.ready,
		ProposedTarget: -1,
		Reasons:        []string{},
	}

	// 1) 冷启动副本到达就绪时刻：转为就绪；就绪期间不做别的动作。
	if e.pending > 0 && nowSeconds >= e.readyAt {
		e.ready += e.pending
		d.Reasons = append(d.Reasons,
			formatReady(e.pending, e.readyAt, e.ready))
		e.pending = 0
		e.readyAt = 0
	}
	d.ReadyBefore = e.ready

	// 2) 切分样本到两个观测窗口；同时判断信号存活与窗口完整性。
	upFrom := nowSeconds - e.cfg.UpWindowSeconds + 1
	downFrom := nowSeconds - e.cfg.DownWindowSeconds + 1
	latestIdx := -1
	for i := range samples {
		s := samples[i]
		if s.TimeSeconds > nowSeconds {
			break
		}
		latestIdx = i
		us := UsedSample{Sample: s}
		// 新鲜度：相对决策时刻的年龄不超过 FreshnessSeconds。
		us.Fresh = !s.Missing && nowSeconds-s.TimeSeconds <= e.cfg.FreshnessSeconds
		if !s.Missing && s.TimeSeconds >= upFrom {
			us.InUpWindow = true
		}
		if !s.Missing && s.TimeSeconds >= downFrom {
			us.InDownWindow = true
		}
		d.UsedSamples = append(d.UsedSamples, us)
	}

	// 3) 信号存活：最近样本缺失或过期 → 保持，绝不按零负载缩容。
	if latestIdx < 0 {
		return e.finishHold(&d, "尚无任何指标样本，保持副本数（缺失不当零负载）")
	}
	latest := samples[latestIdx]
	if latest.Missing {
		return e.finishHold(&d, "最近样本指标缺失，保持副本数（缺失不当零负载）")
	}
	if nowSeconds-latest.TimeSeconds > e.cfg.FreshnessSeconds {
		return e.finishHold(&d, formatStale(latest.TimeSeconds, nowSeconds,
			e.cfg.FreshnessSeconds))
	}
	d.SignalOK = true
	d.LatestLoad = latest.TotalLoad

	// 4) 冷启动门禁：初始冷启动窗口，或有副本尚未就绪。
	if e.pending > 0 {
		return e.finishHold(&d, formatPending(e.pending, e.readyAt))
	}
	if nowSeconds < e.cfg.ColdStartDelaySeconds {
		return e.finishHold(&d, formatColdGate(nowSeconds, e.cfg.ColdStartDelaySeconds))
	}

	// 5) 计算窗口内期望副本。
	//    扩容：短窗口取最高负载（最敏感）；缩容：长窗口取最高负载（最保守）。
	var upMax, downMax float64
	upCount, downCount := 0, 0
	upSeen, downSeen := false, false
	for _, us := range d.UsedSamples {
		if us.Missing {
			continue
		}
		if us.InUpWindow {
			if !upSeen || us.TotalLoad > upMax {
				upMax = us.TotalLoad
			}
			upSeen = true
			upCount++
		}
		if us.InDownWindow {
			if !downSeen || us.TotalLoad > downMax {
				downMax = us.TotalLoad
			}
			downSeen = true
			downCount++
		}
	}
	d.UpWindowMaxLoad = upMax
	d.DownWindowMaxLoad = downMax

	// 缩容窗口完整性：窗口须被连续、新鲜、非缺失的样本覆盖。
	d.DownWindowComplete = downCount > 0 && windowComplete(samples, downFrom,
		nowSeconds, e.cfg.FreshnessSeconds, e.cfg.MaxSampleGapSeconds)

	rawUp := ceilDiv(upMax, e.cfg.LoadPerReplica)
	rawDown := ceilDiv(downMax, e.cfg.LoadPerReplica)
	d.RawUpDesired = rawUp
	d.RawDownDesired = rawDown

	// 6) 形成方向建议（扩容优先）。
	//    direction 唯一确定后续使用哪一路原始期望值：
	//    +1 用 rawUp，-1 用 rawDown（且要求窗口完整），0 表示两路都不构成动作。
	direction := 0 // +1 扩, -1 缩, 0 无需
	rawDesired := e.ready
	downBlocked := false // 长窗口期望更低但窗口不完整
	switch {
	case rawUp > e.ready:
		direction, rawDesired = +1, rawUp
	case rawDown < e.ready:
		if d.DownWindowComplete {
			direction, rawDesired = -1, rawDown
		} else {
			downBlocked = true
			d.Reasons = append(d.Reasons,
				formatDownBlocked(rawUp, rawDown, e.ready))
		}
	}

	// 钳制到 [min,max]；无论是否执行动作，ProposedTarget 都记录“若不受
	// 迟滞/冷却/限速约束时的期望副本”，用于审计。
	target := rawDesired
	clamped := false
	if target < e.cfg.MinReplicas {
		target = e.cfg.MinReplicas
		clamped = true
	}
	if target > e.cfg.MaxReplicas {
		target = e.cfg.MaxReplicas
		clamped = true
	}
	d.Clamped = clamped
	d.ProposedTarget = target
	if direction != 0 {
		d.Reasons = append(d.Reasons, formatWindowBasis(direction, rawDesired, target,
			clamped, upCount, downCount, upMax, downMax))
	} else if !downBlocked {
		d.Reasons = append(d.Reasons, formatNoop(rawUp, rawDown, e.ready))
	}

	// 7) 迟滞带：相对变化必须越过阈值，避免在阈值附近抖动。
	if direction > 0 {
		ratio := float64(target) / float64(e.ready)
		if ratio < 1+e.cfg.UpThresholdPercent/100.0-1e-9 {
			direction = 0
			d.Reasons = append(d.Reasons, formatHysteresis(true, e.ready, target,
				e.cfg.UpThresholdPercent))
		}
	} else if direction < 0 {
		ratio := float64(target) / float64(e.ready)
		if ratio > 1-e.cfg.DownThresholdPercent/100.0+1e-9 {
			direction = 0
			d.Reasons = append(d.Reasons, formatHysteresis(false, e.ready, target,
				e.cfg.DownThresholdPercent))
		}
	}

	// 8) 冷却与稳定窗口。
	if direction > 0 {
		if e.hasUp {
			if wait := e.cfg.UpCooldownSeconds - (nowSeconds - e.lastUp); wait > 0 {
				direction = 0
				d.Reasons = append(d.Reasons, formatCooldown(true, wait,
					e.cfg.UpCooldownSeconds))
			}
		}
	}
	if direction < 0 {
		if e.hasDown {
			if wait := e.cfg.DownCooldownSeconds - (nowSeconds - e.lastDown); wait > 0 {
				direction = 0
				d.Reasons = append(d.Reasons, formatCooldown(false, wait,
					e.cfg.DownCooldownSeconds))
			}
		}
		if direction < 0 && e.hasUp {
			if wait := e.cfg.ScaleUpStabilizationSeconds - (nowSeconds - e.lastUp); wait > 0 {
				direction = 0
				d.Reasons = append(d.Reasons, formatStabilization(wait,
					e.cfg.ScaleUpStabilizationSeconds))
			}
		}
	}

	// 9) 单步限速与执行。
	switch direction {
	case +1:
		add := minInt(target-e.ready, e.cfg.MaxUpStep)
		e.pending = add
		e.readyAt = nowSeconds + e.cfg.ColdStartDelaySeconds
		e.lastUp = nowSeconds
		e.hasUp = true
		d.Action = ActionUp
		d.Reasons = append(d.Reasons, formatUp(add, e.ready, e.cfg.MaxUpStep,
			e.cfg.ColdStartDelaySeconds, e.readyAt))
	case -1:
		remove := minInt(e.ready-target, e.cfg.MaxDownStep)
		e.ready -= remove
		e.lastDown = nowSeconds
		e.hasDown = true
		d.Action = ActionDown
		d.Reasons = append(d.Reasons, formatDown(remove, e.ready,
			e.cfg.MaxDownStep, e.cfg.DownCooldownSeconds))
	default:
		d.Action = ActionHold
		if len(d.Reasons) == 0 {
			d.Reasons = append(d.Reasons,
				fmt.Sprintf("扩/缩窗口期望均未越过当前就绪 %d，无需动作", e.ready))
		}
		// 未执行的期望不作为本步目标：实际目标维持当前总副本。
		// ProposedTarget 保留第 6 步记录的（钳制后）期望值，用于审计“想动但被拦截”的情况。
	}

	d.ReadyAfter = e.ready
	d.PendingAfter = e.pending
	d.TargetReplicas = e.ready + e.pending
	return d
}

// finishHold 以“保持”结束本步（用于门禁类早退）。
func (e *Engine) finishHold(d *Decision, reason string) Decision {
	d.Action = ActionHold
	d.ReadyAfter = e.ready
	d.PendingAfter = e.pending
	d.TargetReplicas = e.ready + e.pending
	d.Reasons = append(d.Reasons, reason)
	return *d
}

// windowComplete 判断 [from,to] 是否被连续、非缺失样本完整覆盖。
//
// 覆盖模型：一个 t 时刻采集的非缺失样本，其数据在 [t, t+freshness] 内有效；
// 相邻样本间隔不超过 maxGap 才算“连续”。因此窗口完整当且仅当：
//   - 窗口内不出现缺失样本（显式缺失会在覆盖上戳洞，绝不按零值补洞）；
//   - 左边界被覆盖：窗口前最后一个非缺失样本（或窗口内首个样本）距 from 不超过 maxGap；
//   - 相邻非缺失样本间隔均不超过 maxGap；
//   - 右边界被覆盖：最后一个样本距 to 不超过 freshness。
func windowComplete(samples []Sample, from, to, freshness, maxGap int64) bool {
	type pt = Sample
	var pts []pt
	for _, s := range samples {
		if s.TimeSeconds > to {
			break
		}
		if s.TimeSeconds >= from {
			pts = append(pts, s)
		}
	}
	// 窗口内任何缺失样本都使窗口不完整。
	for _, s := range pts {
		if s.Missing {
			return false
		}
	}
	if len(pts) == 0 {
		return false
	}
	// 找窗口前最后一个非缺失样本作为左边界兜底。
	var guard *Sample
	for i := len(samples) - 1; i >= 0; i-- {
		s := samples[i]
		if s.TimeSeconds < from && !s.Missing {
			s2 := s
			guard = &s2
			break
		}
	}
	first := pts[0]
	if guard != nil {
		if first.TimeSeconds-guard.TimeSeconds > maxGap {
			return false
		}
	} else if first.TimeSeconds-from > maxGap {
		return false
	}
	prev := first.TimeSeconds
	for _, s := range pts[1:] {
		if s.TimeSeconds-prev > maxGap {
			return false
		}
		prev = s.TimeSeconds
	}
	return to-prev <= freshness
}

// ValidateSamples 校验样本序列：时间戳非负、严格升序。
func ValidateSamples(samples []Sample) error {
	var prev int64 = -1
	for i, s := range samples {
		if s.TimeSeconds < 0 {
			return errors.New("样本时间戳不能为负")
		}
		if i > 0 && s.TimeSeconds <= prev {
			return errors.New("样本时间戳必须严格升序且唯一")
		}
		prev = s.TimeSeconds
	}
	return nil
}

// SortedSamples 返回按时间升序排列的样本副本。
func SortedSamples(in []Sample) []Sample {
	out := append([]Sample(nil), in...)
	sort.SliceStable(out, func(i, j int) bool {
		return out[i].TimeSeconds < out[j].TimeSeconds
	})
	return out
}

func ceilDiv(load, per float64) int {
	if load <= 0 {
		return 0
	}
	return int(math.Ceil(load/per - 1e-9))
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
