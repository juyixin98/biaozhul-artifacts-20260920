// Package engine 是定时触发器的调度核心：维护触发器集合，沿时间轴推进，
// 产出触发事件。时间来源通过 Clock 注入，核心推进逻辑 Advance(now) 与
// 真实时钟解耦——测试用显式时间轴驱动，绝不依赖真实睡眠。
package engine

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sort"
	"sync"
	"time"

	"tztrigger/internal/schedule"
	"tztrigger/internal/tzdb"
)

// Clock 抽象时间来源，生产环境用 RealClock，测试用假时钟。
type Clock interface {
	Now() time.Time
}

// RealClock 生产时钟。
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

// Trigger 是一个已注册的定时触发器。
type Trigger struct {
	ID           string
	Minutes      string // 原始表达式文本
	Hours        string
	Weekdays     string
	Timezone     string // IANA 名称，按内嵌固定版本 tz 数据库解析
	CatchUpLimit int    // 单次推进最多补触发的错过次数；0 = 不补触发
	CreatedAt    time.Time

	Expr *schedule.Expr
	Loc  *time.Location

	// 运行统计
	TotalFired    int // 实际触发次数（去重后）
	MissedDropped int // 因补触发上限被丢弃的错过次数
	DedupSkipped  int // 因逻辑触发 ID 已存在而被跳过的次数
}

// Event 是一次触发记录。
type Event struct {
	Seq        int64     `json:"seq"`
	TriggerID  string    `json:"trigger_id"`
	LogicalID  string    `json:"logical_id"` // 逻辑触发 ID：触发器ID@本地墙钟
	WallClock  string    `json:"wall_clock"` // 本地墙钟时刻 "2006-01-02T15:04"
	Timezone   string    `json:"timezone"`
	FiredAtUTC time.Time `json:"fired_at_utc"` // 实际对应的瞬间（重复时刻取较早一次）
	CaughtUp   bool      `json:"caught_up"`    // true 表示这是对错过时刻的补触发
}

// LogicalID 计算逻辑触发 ID。同一触发器在同一本地墙钟时刻只对应一个 ID：
// 秋季重复时刻的两次出现共享同一 ID，因此只会执行较早一次。
func LogicalID(triggerID string, at time.Time, loc *time.Location) string {
	return triggerID + "@" + schedule.WallClock(at, loc)
}

const (
	maxEventsKept   = 10000          // 事件日志环形上限
	dedupKeepWindow = 48 * time.Hour // 逻辑 ID 去重表保留窗口
	maxMissedCount  = 1 << 20        // 错过窗口枚举的保险上限（约逐分钟 2 年）
)

// Engine 调度引擎。全部状态在内存中（进程重启即丢失，见 README 限制说明）。
type Engine struct {
	clock        Clock
	tickInterval time.Duration

	mu         sync.Mutex
	triggers   map[string]*Trigger
	firedLogic map[string]time.Time // 逻辑触发 ID -> 记录时间（用于去重与清理）
	events     []Event              // 环形日志，超出上限丢弃最旧
	seq        int64
	checkpoint time.Time // 已推进到的时间点（半开区间右端）
}

// New 创建引擎。checkpoint 初始化为 clock.Now()：创建引擎之前的
// 历史时刻不会补触发；之后创建的触发器从当前时刻开始生效。
func New(clock Clock, tickInterval time.Duration) *Engine {
	return &Engine{
		clock:        clock,
		tickInterval: tickInterval,
		triggers:     map[string]*Trigger{},
		firedLogic:   map[string]time.Time{},
		checkpoint:   clock.Now(),
	}
}

// AddTrigger 校验并注册触发器。expr 三个字段为 cron 风格表达式，
// timezone 为 IANA 名称，catchUpLimit 为补触发上限（<0 报错）。
func (e *Engine) AddTrigger(id, minutes, hours, weekdays, timezone string, catchUpLimit int) (*Trigger, error) {
	expr, err := schedule.Parse(minutes, hours, weekdays)
	if err != nil {
		return nil, err
	}
	loc, err := tzdb.LoadLocation(timezone)
	if err != nil {
		return nil, err
	}
	if catchUpLimit < 0 || catchUpLimit > 100000 {
		return nil, fmt.Errorf("catch_up_limit 必须在 [0, 100000] 内")
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if id == "" {
		id = "trig-" + randHex(4)
	}
	if _, dup := e.triggers[id]; dup {
		return nil, fmt.Errorf("触发器 ID %q 已存在", id)
	}
	t := &Trigger{
		ID: id, Minutes: minutes, Hours: hours, Weekdays: weekdays,
		Timezone: timezone, CatchUpLimit: catchUpLimit,
		CreatedAt: e.clock.Now(), Expr: expr, Loc: loc,
	}
	e.triggers[id] = t
	return t, nil
}

// DeleteTrigger 删除触发器，返回是否存在。
func (e *Engine) DeleteTrigger(id string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.triggers[id]; !ok {
		return false
	}
	delete(e.triggers, id)
	return true
}

// GetTrigger 按 ID 查询。
func (e *Engine) GetTrigger(id string) (*Trigger, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	t, ok := e.triggers[id]
	return t, ok
}

// ListTriggers 返回按 ID 排序的全部触发器。
func (e *Engine) ListTriggers() []*Trigger {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]*Trigger, 0, len(e.triggers))
	for _, t := range e.triggers {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// NextFire 计算触发器在 after 之后的下一次触发瞬间（含 after）。
// 表达式只含分钟/小时/周几，最长 8 天内必有一次命中。
func (e *Engine) NextFire(t *Trigger, after time.Time) (time.Time, bool) {
	occ, _ := t.Expr.Between(t.Loc, after, after.Add(8*24*time.Hour), 1)
	if len(occ) == 0 {
		return time.Time{}, false
	}
	return occ[0], true
}

// Preview 枚举触发器在 [from, to) 内的触发瞬间（上限 max），供 API 预览。
func (e *Engine) Preview(t *Trigger, from, to time.Time, max int) ([]time.Time, bool) {
	return t.Expr.Between(t.Loc, from, to, max)
}

// Advance 把时间轴从内部 checkpoint 推进到 now，处理 [checkpoint, now)
// 内所有到期的触发器。这是调度的唯一入口，生产上由 Run 按 tick 周期
// 调用，测试直接以显式时间调用（无需真实睡眠）。
//
// 区间被切成两段：
//   - 错过窗口 [checkpoint, now-tick)：停机/卡顿期间错过的时刻，
//     每个触发器最多补触发 CatchUpLimit 次（保留最新的若干次，
//     陈旧的补触发通常已无意义），超出部分计入 MissedDropped；
//     CatchUpLimit=0 表示完全不补触发；
//   - 正常窗口 [now-tick, now)：本周期内按时到期的时刻，始终触发，
//     不受补触发上限影响（否则 tick 抖动会错误吞掉正常触发）。
func (e *Engine) Advance(now time.Time) {
	e.mu.Lock()
	defer e.mu.Unlock()
	from := e.checkpoint
	if !now.After(from) {
		if now.Before(from) {
			// 时钟回拨：重置 checkpoint，重叠窗口由逻辑 ID 去重兜底
			e.checkpoint = now
		}
		return
	}

	// 错过窗口与正常窗口的分界。钳制到 from 之后，保证首次推进时
	// （from 接近 now）正常窗口不为空且不含历史。
	boundary := now.Add(-e.tickInterval)
	if boundary.Before(from) {
		boundary = from
	}

	type pending struct {
		trig     *Trigger
		at       time.Time
		caughtUp bool
	}
	var pend []pending
	ids := make([]string, 0, len(e.triggers))
	for id := range e.triggers {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		trig := e.triggers[id]

		// 触发器创建之前的时刻不属于“错过”，不补触发
		trigFrom := from
		if trig.CreatedAt.After(trigFrom) {
			trigFrom = trig.CreatedAt
		}

		// 错过窗口：完整枚举（按天推进，停机一个月、逐分钟表达式
		// 也仅数万候选），丢弃最旧的、补触发最新 CatchUpLimit 次
		if boundary.After(trigFrom) {
			occ, _ := trig.Expr.Between(trig.Loc, trigFrom, boundary, maxMissedCount)
			if len(occ) > trig.CatchUpLimit {
				trig.MissedDropped += len(occ) - trig.CatchUpLimit
				occ = occ[len(occ)-trig.CatchUpLimit:]
			}
			for _, at := range occ {
				pend = append(pend, pending{trig, at, true})
			}
		}

		// 正常窗口：一个 tick（最长 1 小时）内逐分钟最多 60 次命中
		normalFrom := boundary
		if trigFrom.After(normalFrom) {
			normalFrom = trigFrom
		}
		occ, _ := trig.Expr.Between(trig.Loc, normalFrom, now, 60)
		for _, at := range occ {
			pend = append(pend, pending{trig, at, false})
		}
	}
	sort.Slice(pend, func(i, j int) bool {
		if !pend[i].at.Equal(pend[j].at) {
			return pend[i].at.Before(pend[j].at)
		}
		return pend[i].trig.ID < pend[j].trig.ID
	})

	for _, p := range pend {
		lid := LogicalID(p.trig.ID, p.at, p.trig.Loc)
		if _, dup := e.firedLogic[lid]; dup {
			p.trig.DedupSkipped++
			continue
		}
		e.firedLogic[lid] = now
		e.seq++
		e.events = append(e.events, Event{
			Seq:        e.seq,
			TriggerID:  p.trig.ID,
			LogicalID:  lid,
			WallClock:  schedule.WallClock(p.at, p.trig.Loc),
			Timezone:   p.trig.Timezone,
			FiredAtUTC: p.at.UTC(),
			CaughtUp:   p.caughtUp,
		})
		p.trig.TotalFired++
	}
	if len(e.events) > maxEventsKept {
		e.events = append([]Event(nil), e.events[len(e.events)-maxEventsKept:]...)
	}
	// 清理过期的去重记录，避免无限增长
	cutoff := now.Add(-dedupKeepWindow)
	for lid, at := range e.firedLogic {
		if at.Before(cutoff) {
			delete(e.firedLogic, lid)
		}
	}
	e.checkpoint = now
}

// Run 按 tick 周期驱动 Advance，直到 ctx 取消。
func (e *Engine) Run(ctx context.Context) {
	t := time.NewTicker(e.tickInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			e.Advance(e.clock.Now())
		}
	}
}

// Events 返回触发事件（按时间升序）。triggerID 为空表示全部；
// limit<=0 表示使用默认上限 1000。
func (e *Engine) Events(triggerID string, limit int) []Event {
	e.mu.Lock()
	defer e.mu.Unlock()
	if limit <= 0 || limit > maxEventsKept {
		limit = maxEventsKept
	}
	out := make([]Event, 0, limit)
	for _, ev := range e.events {
		if triggerID != "" && ev.TriggerID != triggerID {
			continue
		}
		out = append(out, ev)
	}
	if len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
