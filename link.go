package rt

import (
	"context"
	"math/rand"
	"sync"
	"time"
)

// ErrLinkClosed 在链路关闭后继续收发时返回。
var ErrLinkClosed = errClosed{}

type errClosed struct{}

func (errClosed) Error() string { return "rt: link closed" }

// Link 是数据报文收发的抽象。实现方负责“把报文送到对端”，
// 内存 Pipe 与真实 UDP 都实现该接口。
type Link interface {
	// Send 发送一个报文。报文内容在返回后可被调用方复用，
	// 实现方必须自行拷贝需要保留的数据。
	Send(p *Packet) error
	// Recv 阻塞直到收到报文；ctx 取消或链路关闭时返回错误。
	Recv(ctx context.Context) (*Packet, error)
	// Close 关闭链路：尽力冲刷缓存中的报文（如乱序缓存），
	// 之后 Recv 在取完已排队报文后返回 ErrLinkClosed。
	Close() error
}

// FaultPolicy 描述单向链路上注入的故障。所有速率都是 [0,1]（1=必中）。
//
// 除概率外支持“预算”，用于构造“有限丢包”场景：预算为 -1 表示不限，
// 为 0 表示关闭该类故障，为正数表示最多发生多少次。
// 这样验收测试可以保证传输必然完成（故障总量有上界）。
type FaultPolicy struct {
	Seed int64
	// LossRate + LossBudget：丢包概率与最大丢包数（-1 不限，0 关）。
	LossRate   float64
	LossBudget int
	// DupRate + DupBudget：重复（同一份报文投递两次）。
	DupRate   float64
	DupBudget int
	// ReorderRate + ReorderBudget：乱序。被抽中的报文被扣留，
	// 在它之后经过 ReorderHold 个“后续”报文后再投递（后续报文因此先到）。
	// 为防止连接尾部（如 FIN/FINACK）扣留后永久等不到后续报文，
	// 同时设置 ReorderDelay 作为延迟上界：超时后无条件冲刷。
	ReorderRate   float64
	ReorderBudget int
	ReorderHold   int
	// ReorderDelay 是乱序扣留的最长延迟；<=0 时由构造方给默认值。
	// 必须用协议可注入的时钟计时，FakeClock 下确定性触发。
	ReorderDelay time.Duration
	// Ghosts：旧连接代际的“幽灵报文”，会在前若干次发送时穿插注入
	// 到对端。用于验证连接代际隔离。
	Ghosts []*Packet
}

// FaultStats 是单向链路实际发生的故障计数。
type FaultStats struct {
	Offered    int64 // 经过该方向的正常报文数
	Delivered  int64 // 实际投递的报文份数（重复计两份）
	Lost       int64
	Duplicated int64 // 额外多投的份数
	Reordered  int64 // 被扣留过的报文数
	GhostSent  int64 // 注入的旧代际报文数
}

// Injector 是单向故障注入层：包住一个 deliver 回调即可装饰任意链路。
type Injector struct {
	policy    FaultPolicy
	rng       *rand.Rand
	clock     Clock
	holdMax   time.Duration
	deliver   func(*Packet)
	holdMu    sync.Mutex
	holdTimer Timer // 当前在跑的冲刷定时器（至多一个）
	ctx       context.Context
	cancel    context.CancelFunc
	wg        sync.WaitGroup

	mu     sync.Mutex
	closed bool
	held   *Packet
	passed int // held 扣留后通过的后续报文数
	ghosts []*Packet
	stat   FaultStats

	lossLeft         int
	dupLeft          int
	reorderLeft      int
	lossUnlimited    bool
	dupUnlimited     bool
	reorderUnlimited bool
}

// NewInjector 创建故障注入器。clock 用于给乱序扣留设置延迟上界
// （FakeClock 下确定性触发，RealClock 下为真实计时）。
func NewInjector(policy FaultPolicy, clock Clock, deliver func(*Packet)) *Injector {
	hold := policy.ReorderHold
	if hold < 1 {
		hold = 1
	}
	policy.ReorderHold = hold
	if clock == nil {
		clock = RealClock{}
	}
	delay := policy.ReorderDelay
	if delay <= 0 {
		delay = 5 * time.Millisecond
	}
	ctx, cancel := context.WithCancel(context.Background())
	in := &Injector{
		policy:  policy,
		rng:     rand.New(rand.NewSource(policy.Seed)),
		clock:   clock,
		holdMax: delay,
		deliver: deliver,
		ghosts:  policy.Ghosts,
		ctx:     ctx,
		cancel:  cancel,
	}
	in.wg.Add(1)
	go in.holdWatcher()
	in.lossUnlimited = policy.LossBudget < 0
	in.lossLeft = policy.LossBudget
	in.dupUnlimited = policy.DupBudget < 0
	in.dupLeft = policy.DupBudget
	in.reorderUnlimited = policy.ReorderBudget < 0
	in.reorderLeft = policy.ReorderBudget
	return in
}

// Egress 处理一个外发报文（可穿插幽灵报文）。
func (in *Injector) Egress(p *Packet) {
	clone := clonePacket(p)

	in.mu.Lock()
	if in.closed {
		in.mu.Unlock()
		return
	}
	// 幽灵报文与正常报文穿插注入（每条正常报文前放一条，直到放完）。
	var ghost *Packet
	if len(in.ghosts) > 0 {
		ghost = clonePacket(in.ghosts[0])
		in.ghosts = in.ghosts[1:]
		in.stat.GhostSent++
	}
	in.stat.Offered++
	in.mu.Unlock()

	if ghost != nil {
		in.deliver(ghost)
	}

	if in.draw(in.policy.LossRate, in.lossUnlimited, &in.lossLeft) {
		in.mu.Lock()
		in.stat.Lost++
		in.mu.Unlock()
		return
	}

	in.mu.Lock()
	if in.held == nil && in.drawLocked(in.policy.ReorderRate, in.reorderUnlimited, &in.reorderLeft) {
		in.held = clone
		in.passed = 0
		in.stat.Reordered++
		in.mu.Unlock()
		in.scheduleHoldFlush()
		return
	}
	in.mu.Unlock()

	in.deliverWithDups(clone)
	in.advanceHeld()
}

// holdWatcher 是每个注入器唯一的冲刷 goroutine：轮询当前冲刷定时器，
// 到期即冲刷乱序缓存。用 ctx 结束，避免为每次扣留新建 goroutine。
// FakeClock 下 C 的读取只在时钟被 Step/Advance 推进时返回；
// RealClock 下为真实计时。
func (in *Injector) holdWatcher() {
	defer in.wg.Done()
	for {
		if in.ctx.Err() != nil {
			return
		}
		in.holdMu.Lock()
		t := in.holdTimer
		in.holdMu.Unlock()
		if t == nil {
			select {
			case <-in.ctx.Done():
				return
			case <-time.After(100 * time.Microsecond):
			}
			continue
		}
		// 等待 t 到期；同时用真实小周期核对它是否已被提前取消/替换
		// （hold 数先达到时 cancelHoldFlush 会停掉它，t.C 永远不再有值）。
		watch := time.NewTicker(100 * time.Microsecond)
	waitTimer:
		for {
			select {
			case <-in.ctx.Done():
				watch.Stop()
				return
			case <-t.C():
				break waitTimer
			case <-watch.C:
				in.holdMu.Lock()
				cur := in.holdTimer
				in.holdMu.Unlock()
				if cur != t {
					// 已被取消或换成新定时器；回到外层重新观察。
					break waitTimer
				}
			}
		}
		watch.Stop()
		in.holdMu.Lock()
		stillOurs := in.holdTimer == t
		if stillOurs {
			in.holdTimer = nil
		}
		in.holdMu.Unlock()
		if stillOurs {
			in.Flush()
		}
	}
}

// scheduleHoldFlush 为当前被扣留的报文启动一个一次性冲刷定时器，
// 保证乱序注入有延迟上界（连接尾部也不会永久扣留）。
func (in *Injector) scheduleHoldFlush() {
	in.holdMu.Lock()
	defer in.holdMu.Unlock()
	if in.holdTimer != nil {
		return
	}
	in.holdTimer = in.clock.NewTimer(in.holdMax)
}

func (in *Injector) cancelHoldFlush() {
	in.holdMu.Lock()
	if in.holdTimer != nil {
		in.holdTimer.Stop()
		in.holdTimer = nil
	}
	in.holdMu.Unlock()
}

// Flush 冲刷乱序缓存（用于超时冲刷与链路关闭）。
func (in *Injector) Flush() {
	in.mu.Lock()
	h := in.held
	in.held = nil
	in.passed = 0
	in.mu.Unlock()
	if h != nil {
		in.cancelHoldFlush()
		in.deliverWithDups(h)
	}
}

// Close 标记关闭，冲刷缓存，之后 Egress 静默丢弃。
func (in *Injector) Close() {
	in.mu.Lock()
	if in.closed {
		in.mu.Unlock()
		return
	}
	in.closed = true
	h := in.held
	in.held = nil
	in.passed = 0
	in.mu.Unlock()
	in.cancelHoldFlush()
	in.cancel()
	in.wg.Wait() // 先确保 watcher 已退出，再投递残留报文，避免重复/竞争
	if h != nil {
		in.deliverWithDups(h)
	}
}

func (in *Injector) advanceHeld() {
	in.mu.Lock()
	if in.held == nil {
		in.mu.Unlock()
		return
	}
	in.passed++
	if in.passed < in.policy.ReorderHold {
		in.mu.Unlock()
		return
	}
	h := in.held
	in.held = nil
	in.passed = 0
	in.mu.Unlock()
	in.cancelHoldFlush()
	in.deliverWithDups(h)
}

// deliverWithDups 按重复概率投递 1 份或 2 份。
func (in *Injector) deliverWithDups(p *Packet) {
	in.mu.Lock()
	dup := in.drawLocked(in.policy.DupRate, in.dupUnlimited, &in.dupLeft)
	in.stat.Delivered++
	if dup {
		in.stat.Duplicated++
		in.stat.Delivered++
	}
	in.mu.Unlock()

	in.deliver(p)
	if dup {
		in.deliver(clonePacket(p))
	}
}

func (in *Injector) draw(rate float64, unlimited bool, left *int) bool {
	in.mu.Lock()
	defer in.mu.Unlock()
	return in.drawLocked(rate, unlimited, left)
}

// 调用方持锁。
func (in *Injector) drawLocked(rate float64, unlimited bool, left *int) bool {
	if rate <= 0 || (!unlimited && *left <= 0) {
		return false
	}
	hit := in.rng.Float64() < rate
	if hit && !unlimited {
		*left--
	}
	return hit
}

// Stats 返回故障计数快照。
func (in *Injector) Stats() FaultStats {
	in.mu.Lock()
	defer in.mu.Unlock()
	return in.stat
}

func clonePacket(p *Packet) *Packet {
	c := &Packet{Type: p.Type, Seq: p.Seq, Ack: p.Ack, Gen: p.Gen}
	if len(p.Payload) > 0 {
		c.Payload = make([]byte, len(p.Payload))
		copy(c.Payload, p.Payload)
	}
	return c
}

// ---- 内存链路 ----

type mailbox struct {
	mu     sync.Mutex
	q      []*Packet
	closed bool
	cond   sync.Cond
}

func newMailbox() *mailbox {
	m := &mailbox{}
	m.cond.L = &m.mu
	return m
}

func (m *mailbox) deliver(p *Packet) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return false
	}
	m.q = append(m.q, p)
	m.cond.Signal()
	return true
}

func (m *mailbox) recv(ctx context.Context) (*Packet, error) {
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			m.mu.Lock()
			m.cond.Broadcast()
			m.mu.Unlock()
		case <-done:
		}
	}()

	m.mu.Lock()
	for len(m.q) == 0 && !m.closed && ctx.Err() == nil {
		m.cond.Wait()
	}
	switch {
	case len(m.q) > 0:
		p := m.q[0]
		m.q = m.q[1:]
		m.mu.Unlock()
		return p, nil
	case ctx.Err() != nil:
		m.mu.Unlock()
		return nil, ctx.Err()
	default:
		m.mu.Unlock()
		return nil, ErrLinkClosed
	}
}

func (m *mailbox) close() {
	m.mu.Lock()
	m.closed = true
	m.cond.Broadcast()
	m.mu.Unlock()
}

// pipeEndpoint 是 Pipe 的一端：外发经 injector 到对端邮箱，接收来自自己邮箱。
type pipeEndpoint struct {
	in      *mailbox // 自己的接收邮箱
	inj     *Injector
	closeMu sync.Mutex
	closed  bool
}

func (e *pipeEndpoint) Send(p *Packet) error {
	e.closeMu.Lock()
	closed := e.closed
	e.closeMu.Unlock()
	if closed {
		return ErrLinkClosed
	}
	e.inj.Egress(p)
	return nil
}

func (e *pipeEndpoint) Recv(ctx context.Context) (*Packet, error) {
	return e.in.recv(ctx)
}

func (e *pipeEndpoint) Close() error {
	e.closeMu.Lock()
	if e.closed {
		e.closeMu.Unlock()
		return nil
	}
	e.closed = true
	e.closeMu.Unlock()

	e.inj.Flush() // 冲刷本端外发方向缓存（送入对端邮箱）
	e.inj.Close()
	e.in.close() // 关闭本端接收方向
	return nil
}

// PipeFaults 是一对方向上的故障策略。
type PipeFaults struct {
	AB FaultPolicy // A 发往 B（数据方向）
	BA FaultPolicy // B 发往 A（ACK 方向）
}

// PipeWithClock 返回一对内存链路端点，并用给定时钟驱动乱序冲刷定时器。
func PipeWithClock(f PipeFaults, clock Clock) (a, b Link) {
	aIn, bIn := newMailbox(), newMailbox()
	epA := &pipeEndpoint{in: aIn, inj: NewInjector(f.AB, clock, func(p *Packet) { bIn.deliver(p) })}
	epB := &pipeEndpoint{in: bIn, inj: NewInjector(f.BA, clock, func(p *Packet) { aIn.deliver(p) })}
	return epA, epB
}

// Pipe 返回一对用内存邮箱连通的链路端点（A 与 B），
// 两个方向各自挂一个故障注入器，使用真实时钟做乱序冲刷上界。
func Pipe(f PipeFaults) (a, b Link) {
	return PipeWithClock(f, RealClock{})
}

// PipeStats 保存两个方向的故障计数。
type PipeStats struct {
	AB FaultStats
	BA FaultStats
}

// StatsOf 读取由 Pipe 创建的端点对的故障计数。对非 Pipe 端点返回 false。
func StatsOf(a, b Link) (PipeStats, bool) {
	ea, ok1 := a.(*pipeEndpoint)
	eb, ok2 := b.(*pipeEndpoint)
	if !ok1 || !ok2 {
		return PipeStats{}, false
	}
	return PipeStats{AB: ea.inj.Stats(), BA: eb.inj.Stats()}, true
}
