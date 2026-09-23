// Package batch 实现“按兼容键合批”的聚合调度器。
//
// 核心模型：
//   - 调用方通过 Submit 提交一个 Request；相同 Key 的请求进入同一批次，
//     不同 Key 完全隔离（各自有独立的等待窗口与缓冲）。
//   - 一个打开的批次在满足任一条件时发批：达到 MaxCount、达到
//     MaxBatchBytes、等待时间达到 MaxWait，或调度器正在关闭。
//   - 单项超过 MaxItemBytes 会在进入批次前被明确拒绝（ErrItemTooLarge），
//     不会污染任何批次。
//   - 单个请求的取消只影响其自身：未执行则从批中摘除，批中其余项照常执行；
//     已交给执行器的批不会因单项取消而中止。
//   - 每次状态转换都通过 EventSink 输出结构化事件。
//
// 时钟（Clock）与执行器（Executor）均为接口，可分别用 FakeClock 与
// 测试执行器替换，从而确定性地验证超时发批、满批与部分失败等行为。
package batch

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// Request 是可合批请求必须实现的最小接口。
type Request[T any] interface {
	// Key 返回合批兼容键：只有相同 Key 的请求才会进入同一批次。
	Key() string
	// Size 返回请求的逻辑字节数，用于 MaxBatchBytes/MaxItemBytes 约束。
	Size() int
}

// ItemResult 是执行器对批中单个请求的处理结果。
type ItemResult struct {
	// Output 为成功时的返回载荷。
	Output map[string]any
	// Err 非空表示该单项失败，不影响批中其它项。
	Err error
}

// Executor 执行一个已关闭的批次。
// 返回整体 error 表示整批失败（如后端不可用），批中每个请求都会收到失败；
// 返回 nil 时，结果切片长度必须与入批条目数一致，逐项对应，
// 单项错误通过 ItemResult.Err 表达（部分失败）。
//
// 注意：传入的 ctx 不会绑定任何单项请求的生命周期——单项取消不得中止整批。
type Executor[T any] interface {
	Execute(ctx context.Context, items []T) []ItemResult
}

// Config 是 Batcher 的配置。
type Config struct {
	// MaxCount 为单批最大条数，必须 > 0。
	MaxCount int
	// MaxBatchBytes 为单批累计最大字节数，必须 > 0。
	MaxBatchBytes int
	// MaxItemBytes 为单项字节上限；超过的请求被直接拒绝。必须 > 0。
	MaxItemBytes int
	// MaxWait 为从批中第一个请求到达起的最长等待时间。必须 > 0。
	MaxWait time.Duration
	// Clock 为时间源；为 nil 时使用真实墙钟。
	Clock Clock
	// Sink 为事件接收器；为 nil 时丢弃事件。
	Sink EventSink
}

func (c Config) withDefaults() (Config, error) {
	if c.MaxCount <= 0 {
		return c, fmt.Errorf("batch: MaxCount must be positive, got %d", c.MaxCount)
	}
	if c.MaxBatchBytes <= 0 {
		return c, fmt.Errorf("batch: MaxBatchBytes must be positive, got %d", c.MaxBatchBytes)
	}
	if c.MaxItemBytes <= 0 {
		return c, fmt.Errorf("batch: MaxItemBytes must be positive, got %d", c.MaxItemBytes)
	}
	if c.MaxItemBytes > c.MaxBatchBytes {
		return c, fmt.Errorf("batch: MaxItemBytes (%d) must not exceed MaxBatchBytes (%d)",
			c.MaxItemBytes, c.MaxBatchBytes)
	}
	if c.MaxWait <= 0 {
		return c, fmt.Errorf("batch: MaxWait must be positive, got %s", c.MaxWait)
	}
	if c.Clock == nil {
		c.Clock = SystemClock{}
	}
	if c.Sink == nil {
		c.Sink = NopSink{}
	}
	return c, nil
}

// Stats 是调度器的计数器快照（只读）。
type Stats struct {
	Submitted    int64          `json:"submitted"`
	Admitted     int64          `json:"admitted"`
	Rejected     int64          `json:"rejected"`
	Canceled     int64          `json:"canceled"`
	Flushed      int64          `json:"flushed"`
	Succeeded    int64          `json:"succeeded"`
	Failed       int64          `json:"failed"`
	FlushReasons map[string]int `json:"flush_reasons"`
	Keys         int            `json:"keys"`
}

// keyChanBuf 是每个合批键的内部缓冲；缓冲满时入队会阻塞，
// 但 Shutdown 在排空时会先读完缓冲，且与入队互斥，因此不会丢消息。
const keyChanBuf = 256

// Batcher 是泛型合批调度器。
type Batcher[T Request[T]] struct {
	cfg  Config
	exec Executor[T]

	keyMu sync.Mutex
	keys  map[string]chan any
	drain bool

	// shutdownCh 关闭表示开始排空：各键循环非阻塞读完已入队消息后
	// 尽快发出残留批次；Submit 也据此拒绝新请求。
	shutdownCh chan struct{}

	sending sync.WaitGroup // 计数处于第二阶段（等待结果）的 Submit
	loops   sync.WaitGroup // 计数各键的调度循环
	execWG  sync.WaitGroup // 计数在途的批次执行

	// submitMu 串行化“关闭检查点”：Shutdown 持锁期间，不会有 Submit
	// 同时越过检查点，保证关闭决策与发送互斥。
	submitMu sync.Mutex

	shutOnce sync.Once

	reqSeq   atomic.Int64
	batchSeq atomic.Int64

	statSubmitted atomic.Int64
	statAdmitted  atomic.Int64
	statRejected  atomic.Int64
	statCanceled  atomic.Int64
	statFlushed   atomic.Int64
	statSucceeded atomic.Int64
	statFailed    atomic.Int64

	reasonMu     sync.Mutex
	flushReasons map[string]int64
}

// New 创建调度器并立即开始接受请求。
func New[T Request[T]](exec Executor[T], cfg Config) (*Batcher[T], error) {
	cfg, err := cfg.withDefaults()
	if err != nil {
		return nil, err
	}
	b := &Batcher[T]{
		cfg:          cfg,
		exec:         exec,
		keys:         make(map[string]chan any),
		shutdownCh:   make(chan struct{}),
		flushReasons: make(map[string]int64),
	}
	return b, nil
}

// ---- 内部消息类型 ----

type entry[T any] struct {
	id   string
	key  string
	size int
	item T

	// mu 保护结果字段与 settled 标志：一个 entry 必须且只能被结算一次，
	// 取消摘条与批次交付因此天然互斥。
	mu      sync.Mutex
	settled bool
	output  map[string]any
	err     error
	// ready 在结算后关闭；Submit 阻塞等待它。
	ready chan struct{}
}

func newEntry[T Request[T]](id, key string, size int, item T) *entry[T] {
	return &entry[T]{id: id, key: key, size: size, item: item, ready: make(chan struct{})}
}

// settle 尝试结算该 entry：首个结算者写入结果并关闭 ready，返回 true；
// 后续（并发取消/执行）调用返回 false，结果不变。
// settleResult 是一次 entry 结算的载荷。
type settleResult struct {
	output map[string]any
	err    error
}

func (e *entry[T]) settle(output map[string]any, err error) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.settled {
		return false
	}
	e.settled = true
	e.output = output
	e.err = err
	close(e.ready)
	return true
}

// result 必须在 ready 关闭后调用。
func (e *entry[T]) result() (map[string]any, error) {
	return e.output, e.err
}

type cancelMsg struct {
	key string
	id  string
}

// ider 是可选接口：请求可提供外部 ID，事件将直接使用它，
// 便于与调用方的请求/响应关联。
type ider interface {
	SchedulerID() string
}

func (b *Batcher[T]) nextRequestID(item T) string {
	if v, ok := any(item).(ider); ok {
		if id := v.SchedulerID(); id != "" {
			return id
		}
	}
	return fmt.Sprintf("req-%d", b.reqSeq.Add(1))
}

// Submit 提交一个请求并阻塞等待其所属批次执行完毕。
//
// 返回值语义：
//   - 成功：执行器返回的 Output；
//   - 单项超大：包装 ErrItemTooLarge 的 *ItemTooLargeError（请求未入批）；
//   - 正在关闭：ErrShuttingDown；
//   - 请求方取消：ctx.Err()；
//   - 该项执行失败：执行器给出的错误（整批失败会被包装）。
func (b *Batcher[T]) Submit(ctx context.Context, item T) (map[string]any, error) {
	size := item.Size()
	if size > b.cfg.MaxItemBytes {
		b.statRejected.Add(1)
		b.emit(Event{
			Type:  EventItemRejected,
			Key:   item.Key(),
			Bytes: size,
			Err:   ErrItemTooLarge.Error(),
			Meta:  map[string]string{"max": fmt.Sprint(b.cfg.MaxItemBytes)},
		})
		return nil, &ItemTooLargeError{Size: size, Max: b.cfg.MaxItemBytes}
	}
	key := item.Key()

	e := newEntry[T](b.nextRequestID(item), key, size, item)

	// 越过关闭检查点并完成入队：submitMu 保证与 Shutdown 的关闭决策互斥，
	// 入队与第二阶段等待者登记都在临界区内完成；Shutdown 只有在临界区
	// 之后才会 Wait 等待者计数，因此不存在 WaitGroup 的 Add/Wait 竞态。
	b.submitMu.Lock()
	if b.isDraining() {
		b.submitMu.Unlock()
		return nil, ErrShuttingDown
	}
	ch, ok := b.keyChannel(key)
	if !ok {
		b.submitMu.Unlock()
		return nil, ErrShuttingDown
	}
	b.statSubmitted.Add(1)
	b.sending.Add(1)
	select {
	case ch <- e:
		b.submitMu.Unlock()
	case <-ctx.Done():
		b.submitMu.Unlock()
		b.sending.Done()
		b.statSubmitted.Add(-1)
		return nil, ctx.Err()
	}
	defer b.sending.Done()

	// 第二阶段：等待批处理结果。此时取消只能摘掉这一个 entry。
	select {
	case <-e.ready:
		out, err := e.result()
		return out, err
	case <-ctx.Done():
		b.sendCancel(ch, e.id)
		return nil, ctx.Err()
	}
}

func (b *Batcher[T]) sendCancel(ch chan any, id string) {
	// 排空期循环仍会读完缓冲并处理该取消；若循环已退出则说明条目
	// 已经执行，取消无意义，直接丢弃——取消永远只影响对应项。
	select {
	case ch <- cancelMsg{id: id}:
	case <-b.shutdownCh:
	}
}

func (b *Batcher[T]) keyChannel(key string) (chan any, bool) {
	// 调用方持有 submitMu；本方法另外通过 keyMu 保护键表。
	b.keyMu.Lock()
	defer b.keyMu.Unlock()
	if b.drain {
		return nil, false
	}
	ch, ok := b.keys[key]
	if !ok {
		ch = make(chan any, keyChanBuf)
		b.keys[key] = ch
		b.loops.Add(1)
		go b.runKey(key, ch)
	}
	return ch, true
}

func (b *Batcher[T]) isDraining() bool {
	b.keyMu.Lock()
	defer b.keyMu.Unlock()
	return b.drain
}

func (b *Batcher[T]) removeKey(key string) {
	b.keyMu.Lock()
	delete(b.keys, key)
	b.keyMu.Unlock()
}

// ---- 每个合批键一个调度循环 ----

type openBatch[T any] struct {
	id      string
	key     string
	entries []*entry[T]
	byID    map[string]*entry[T]
	size    int
	opened  time.Time
	timer   Timer
	// closed 在批被移交给执行器时置位；此后到达的取消消息一律忽略，
	// 条目的终态由执行路径结算。
	closed bool
}

func (b *Batcher[T]) runKey(key string, ch chan any) {
	defer b.loops.Done()
	var cur *openBatch[T]
	for {
		var timerC <-chan time.Time
		if cur != nil {
			timerC = cur.timer.C()
		}
		select {
		case msg := <-ch:
			cur = b.handle(cur, msg)
		case <-timerC:
			b.flush(cur, ReasonTimeout)
			cur = nil
		case <-b.shutdownCh:
			b.drainKey(cur, ch)
			b.removeKey(key)
			return
		}
	}
}

// drainKey 在关闭状态下排空单个键：非阻塞地读完此刻已入队的全部消息，
// 然后以 ReasonDrain 发出尚未发的批。Submit 的入队在关闭决策互斥锁内
// 完成，因此本方法在 shutdownCh 关闭后开始读取时，不会再有新消息抵达。
func (b *Batcher[T]) drainKey(cur *openBatch[T], ch chan any) {
	for {
		select {
		case msg := <-ch:
			cur = b.handle(cur, msg)
		default:
			if cur != nil && len(cur.entries) > 0 {
				b.flush(cur, ReasonDrain)
			} else if cur != nil {
				cur.timer.Stop()
			}
			return
		}
	}
}

// handle 处理键循环收到的一条入队消息；cur 为 nil 时只可能是新条目
// 或一个无法匹配任何在批条目的取消（忽略）。
func (b *Batcher[T]) handle(cur *openBatch[T], msg any) *openBatch[T] {
	switch m := msg.(type) {
	case *entry[T]:
		// 加入当前批会超出字节上限：先发旧批，新项另开一批。
		// 单项超大已在 Submit 入口拒绝，因此新批自身一定放得下。
		if cur != nil && cur.size+m.size > b.cfg.MaxBatchBytes {
			b.flush(cur, ReasonBytes)
			cur = nil
		}
		if cur == nil {
			cur = b.openAndAdmit(m)
		} else {
			b.admit(cur, m)
		}
		if len(cur.entries) >= b.cfg.MaxCount {
			b.flush(cur, ReasonCount)
			return nil
		}
		return cur
	case cancelMsg:
		if cur == nil || cur.closed {
			return cur
		}
		if e, ok := cur.byID[m.id]; ok {
			b.removeEntry(cur, e)
			// settle 保证：若批次执行与取消并发，只有一方生效，
			// 取消绝不可能覆盖已成功的结果（反之亦然）。
			if e.settle(nil, ErrExecCanceled) {
				b.statCanceled.Add(1)
				b.emit(Event{
					Type:      EventItemCanceled,
					Key:       cur.key,
					BatchID:   cur.id,
					RequestID: e.id,
					Count:     len(cur.entries),
				})
			}
			if len(cur.entries) == 0 {
				cur.timer.Stop()
				return nil
			}
		}
		return cur
	default:
		panic(fmt.Sprintf("batch: unknown message type %T", msg))
	}
}

func (b *Batcher[T]) removeEntry(cur *openBatch[T], e *entry[T]) {
	delete(cur.byID, e.id)
	for i, x := range cur.entries {
		if x.id == e.id {
			cur.entries = append(cur.entries[:i], cur.entries[i+1:]...)
			break
		}
	}
	cur.size -= e.size
}

func (b *Batcher[T]) openAndAdmit(e *entry[T]) *openBatch[T] {
	id := fmt.Sprintf("batch-%d", b.batchSeq.Add(1))
	cur := &openBatch[T]{
		id:      id,
		key:     e.key,
		entries: make([]*entry[T], 0, b.cfg.MaxCount),
		byID:    make(map[string]*entry[T]),
		opened:  b.clock().Now(),
		timer:   b.clock().NewTimer(b.cfg.MaxWait),
	}
	b.emit(Event{Type: EventBatchOpened, At: cur.opened, Key: e.key, BatchID: id})
	b.admit(cur, e)
	return cur
}

func (b *Batcher[T]) admit(cur *openBatch[T], e *entry[T]) {
	cur.entries = append(cur.entries, e)
	cur.byID[e.id] = e
	cur.size += e.size
	b.statAdmitted.Add(1)
	b.emit(Event{
		Type:      EventItemAdmitted,
		Key:       cur.key,
		BatchID:   cur.id,
		RequestID: e.id,
		Count:     len(cur.entries),
		Bytes:     cur.size,
	})
}

func (b *Batcher[T]) flush(cur *openBatch[T], reason string) {
	cur.timer.Stop()
	cur.closed = true
	n := len(cur.entries)
	b.statFlushed.Add(1)
	b.bumpReason(reason)
	b.emit(Event{
		Type:    EventBatchFlushed,
		Key:     cur.key,
		BatchID: cur.id,
		Reason:  reason,
		Count:   n,
		Bytes:   cur.size,
	})

	entries := cur.entries
	id, key := cur.id, cur.key
	b.execWG.Add(1)
	go b.executeBatch(id, key, entries)
}

// ---- 批次执行 ----

func (b *Batcher[T]) executeBatch(id, key string, entries []*entry[T]) {
	defer b.execWG.Done()

	// 构造入批快照时跳过已被取消结算的条目：取消先于发批到达的情况下，
	// 这些条目不能进入执行器。flush 之后才到达的取消由 settle 去重。
	live := entries[:0]
	for _, e := range entries {
		e.mu.Lock()
		canceled := e.settled && errors.Is(e.err, ErrExecCanceled)
		e.mu.Unlock()
		if !canceled {
			live = append(live, e)
		}
	}
	entries = live
	if len(entries) == 0 {
		b.emit(Event{Type: EventBatchCompleted, Key: key, BatchID: id, Count: 0})
		return
	}

	items := make([]T, len(entries))
	for i, e := range entries {
		items[i] = e.item
	}

	// 刻意使用 background context：单项取消不得中止整批执行。
	results := b.exec.Execute(context.Background(), items)

	if len(results) != len(entries) {
		err := fmt.Errorf("%w: got %d results for %d items",
			ErrExecutorResultMismatch, len(results), len(entries))
		for _, e := range entries {
			b.deliver(id, e, settleResult{err: err}, false)
		}
		b.emit(Event{Type: EventBatchCompleted, Key: key, BatchID: id, Count: len(entries)})
		return
	}

	for i, r := range results {
		b.deliver(id, entries[i], settleResult{output: r.Output, err: r.Err}, r.Err == nil)
	}
	b.emit(Event{Type: EventBatchCompleted, Key: key, BatchID: id, Count: len(entries)})
}

func (b *Batcher[T]) deliver(batchID string, e *entry[T], o settleResult, ok bool) {
	// 与取消路径竞争：若该 entry 已被取消结算，这里保持其取消结果，
	// 但不发成功/失败事件（取消事件已代表其终态）。
	if !e.settle(o.output, o.err) {
		return
	}
	ev := Event{Key: e.key, BatchID: batchID, RequestID: e.id}
	if ok {
		b.statSucceeded.Add(1)
		ev.Type = EventItemSucceeded
	} else {
		b.statFailed.Add(1)
		ev.Type = EventItemFailed
		ev.Err = errText(o.err)
	}
	b.emit(ev)
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ---- 关闭与观测 ----

// Shutdown 优雅关闭：停止接受新请求，排空所有已入队请求并等待
// 在途批次执行完毕，然后唤醒尚在等待结果的 Submit。
// ctx 超时则返回 ctx.Err()，但各键循环仍保证退出。
func (b *Batcher[T]) Shutdown(ctx context.Context) error {
	b.shutOnce.Do(func() {
		// 持锁期间不会有 Submit 越过检查点：要么此前已完成入队
		// （消息保证被随后的非阻塞排空读到），要么此后看到 drain。
		b.submitMu.Lock()
		b.keyMu.Lock()
		b.drain = true
		b.keyMu.Unlock()
		close(b.shutdownCh)
		b.submitMu.Unlock()

		b.emit(Event{Type: EventDrainStarted})
	})

	// 阶段一：等待键循环完成排空（此时残留批次已交给执行器）。
	if err := waitWithCtx(ctx, &b.loops); err != nil {
		return err
	}
	// 阶段二：等待在途批次执行并交付结果。
	if err := waitWithCtx(ctx, &b.execWG); err != nil {
		return err
	}
	// 阶段三：所有结果已写入 done 通道，等待 Submit 调用方落定。
	b.sending.Wait()
	b.emit(Event{Type: EventDrainCompleted})
	return nil
}

func waitWithCtx(ctx context.Context, wg *sync.WaitGroup) error {
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Stats 返回计数器快照。
func (b *Batcher[T]) Stats() Stats {
	b.reasonMu.Lock()
	reasons := make(map[string]int, len(b.flushReasons))
	for k, v := range b.flushReasons {
		reasons[k] = int(v)
	}
	b.reasonMu.Unlock()
	b.keyMu.Lock()
	n := len(b.keys)
	b.keyMu.Unlock()
	return Stats{
		Submitted:    b.statSubmitted.Load(),
		Admitted:     b.statAdmitted.Load(),
		Rejected:     b.statRejected.Load(),
		Canceled:     b.statCanceled.Load(),
		Flushed:      b.statFlushed.Load(),
		Succeeded:    b.statSucceeded.Load(),
		Failed:       b.statFailed.Load(),
		FlushReasons: reasons,
		Keys:         n,
	}
}

func (b *Batcher[T]) bumpReason(reason string) {
	b.reasonMu.Lock()
	b.flushReasons[reason]++
	b.reasonMu.Unlock()
}

func (b *Batcher[T]) clock() Clock { return b.cfg.Clock }

func (b *Batcher[T]) emit(e Event) {
	if e.At.IsZero() {
		e.At = b.cfg.Clock.Now()
	}
	b.cfg.Sink.OnEvent(e)
}
