// Package event 定义执行器状态变更的结构化事件记录与订阅机制。
//
// 每次任务/执行器的状态变化都会产生一条不可变的 Event。事件先进入
// Bus 的有界环形缓冲，再扇出给所有订阅者；缓冲满时按溢出策略丢弃
// 最旧或最新的事件，保证事件投递永不阻塞工作线程的关键路径。
package event

import (
	"sync"
	"time"
)

// Kind 标识事件类型。
type Kind string

const (
	KindExecutorStarted  Kind = "executor_started"
	KindExecutorStopping Kind = "executor_stopping"
	KindExecutorStopped  Kind = "executor_stopped"
	KindTaskSubmitted    Kind = "task_submitted"
	KindTaskScheduled    Kind = "task_scheduled" // 调度器安排（延迟到期后）提交
	KindTaskDequeued     Kind = "task_dequeued"
	KindTaskStarted      Kind = "task_started"
	KindTaskCompleted    Kind = "task_completed"
	KindTaskFailed       Kind = "task_failed"
	KindTaskCanceled     Kind = "task_canceled"
	KindTaskPanicked     Kind = "task_panicked"
	KindTaskStole        Kind = "task_stole" // 从其他 worker 窃取成功
	KindTaskSpawned      Kind = "task_spawned"
	KindWorkerParked     Kind = "worker_parked"
	KindWorkerWoken      Kind = "worker_woken"
)

// Event 是一条结构化状态变更记录，字段对所有 Kind 复用，未使用字段取零值。
type Event struct {
	Seq      int64          `json:"seq"`
	Time     time.Time      `json:"time"`
	Kind     Kind           `json:"kind"`
	Executor string         `json:"executor"`
	TaskID   string         `json:"task_id,omitempty"`
	ParentID string         `json:"parent_id,omitempty"`
	Worker   int            `json:"worker,omitempty"`
	From     string         `json:"from,omitempty"` // 受害者 worker（窃取事件）
	Err      string         `json:"err,omitempty"`
	Payload  map[string]any `json:"payload,omitempty"`
}

// Sink 消费一条事件。实现必须保证快速返回；阻塞的 Sink 会被 Bus 的
// 异步投递协程串行化，不应在 Sink 中反向调用执行器。
type Sink interface {
	Write(ev Event)
}

// SinkFunc 把普通函数适配为 Sink。
type SinkFunc func(ev Event)

func (f SinkFunc) Write(ev Event) { f(ev) }

// Overflow 定义环形缓冲满时的丢弃策略。
type Overflow int

const (
	// DropOldest 丢弃最旧的事件（默认，订阅者看到近期事件流）。
	DropOldest Overflow = iota
	// DropNewest 丢弃最新到达的事件（保留完整历史前缀）。
	DropNewest
)

// Stats 是事件总线的投递统计快照。
type Stats struct {
	Emitted     int64
	Delivered   int64
	Dropped     int64
	Subscribers int
}

// Bus 是有界的结构化事件总线。
type Bus struct {
	mu       sync.Mutex
	name     string
	buf      []Event
	cap      int
	head     int // 缓冲中最旧元素下标
	size     int
	seq      int64
	subs     map[*subscriber]struct{}
	overflow Overflow
	emitted  int64
	dropped  int64
}

type subscriber struct {
	// ch 容量即订阅者自己的缓冲；投递非阻塞，满了就丢事件并累加 dropped。
	ch     chan Event
	closed bool
}

// Option 配置 Bus。
type Option func(*Bus)

// WithCapacity 设置环形缓冲容量（最小 1）。
func WithCapacity(n int) Option {
	return func(b *Bus) {
		if n > 0 {
			b.cap = n
		}
	}
}

// WithOverflow 设置溢出策略。
func WithOverflow(o Overflow) Option {
	return func(b *Bus) { b.overflow = o }
}

// NewBus 创建事件总线。环形缓冲让新订阅者可先读到最近的历史事件。
func NewBus(executorName string, opts ...Option) *Bus {
	b := &Bus{
		name:     executorName,
		cap:      4096,
		overflow: DropOldest,
		subs:     make(map[*subscriber]struct{}),
	}
	for _, o := range opts {
		o(b)
	}
	b.buf = make([]Event, b.cap)
	return b
}

// Emit 构造并发布一条事件。payload 可省略。永不阻塞、永不 panic。
func (b *Bus) Emit(now time.Time, kind Kind, taskID, parentID string, worker int, err error, payload map[string]any) {
	b.mu.Lock()
	b.seq++
	ev := Event{
		Seq:      b.seq,
		Time:     now,
		Kind:     kind,
		Executor: b.name,
		TaskID:   taskID,
		ParentID: parentID,
		Worker:   worker,
		Payload:  payload,
	}
	if err != nil {
		ev.Err = err.Error()
	}
	b.emitted++

	// 写入环形缓冲。
	if b.size == b.cap {
		if b.overflow == DropNewest {
			b.dropped++
			b.fanoutLocked(ev, true) // 仍尝试实时投给活跃订阅者。
			b.mu.Unlock()
			return
		}
		// DropOldest：覆盖最旧槽位。
		b.head = (b.head + 1) % b.cap
		b.size--
		b.dropped++
	}
	idx := 0
	if b.size > 0 {
		idx = (b.head + b.size) % b.cap
	}
	b.buf[idx] = ev
	b.size++
	b.fanoutLocked(ev, false)
	b.mu.Unlock()
}

// fanoutLocked 非阻塞地把事件投给所有订阅者。调用时持有 b.mu
// （通道发送在缓冲满时立即走 default 分支，不会长时间持锁）。
func (b *Bus) fanoutLocked(ev Event, alreadyDropped bool) {
	for s := range b.subs {
		select {
		case s.ch <- ev:
		default:
			if !alreadyDropped {
				b.dropped++
			}
		}
	}
}

// Subscription 是一个事件订阅句柄。
type Subscription struct {
	bus *Bus
	sub *subscriber
}

// C 返回事件通道，通道关闭后返回零值且 ok=false。
func (s *Subscription) C() <-chan Event { return s.sub.ch }

// Close 取消订阅。
func (s *Subscription) Close() {
	s.bus.removeSubscriber(s.sub)
}

// Subscribe 订阅后续事件，并立即得到最近历史事件的快照（旧 -> 新）。
// subCapacity 是订阅者私有通道缓冲；消费不及时会丢事件。
func (b *Bus) Subscribe(subCapacity int) (*Subscription, []Event) {
	if subCapacity < 1 {
		subCapacity = 1
	}
	s := &subscriber{ch: make(chan Event, subCapacity)}
	b.mu.Lock()
	history := make([]Event, 0, b.size)
	for i := 0; i < b.size; i++ {
		history = append(history, b.buf[(b.head+i)%b.cap])
	}
	b.subs[s] = struct{}{}
	b.mu.Unlock()
	return &Subscription{bus: b, sub: s}, history
}

func (b *Bus) removeSubscriber(s *subscriber) {
	b.mu.Lock()
	if _, ok := b.subs[s]; ok {
		delete(b.subs, s)
		if !s.closed {
			s.closed = true
			close(s.ch)
		}
	}
	b.mu.Unlock()
}

// Stats 返回投递统计快照。
func (b *Bus) Stats() Stats {
	b.mu.Lock()
	defer b.mu.Unlock()
	return Stats{
		Emitted:     b.emitted,
		Dropped:     b.dropped,
		Subscribers: len(b.subs),
	}
}

// Drain 等待订阅者通道被取空的便捷辅助（主要用于测试）：它只是排空快照，
// 并不能保证未来事件；测试中通常配合执行器 Shutdown 后使用。
func (s *Subscription) Drain() []Event {
	var out []Event
	for {
		select {
		case ev, ok := <-s.sub.ch:
			if !ok {
				return out
			}
			out = append(out, ev)
		default:
			return out
		}
	}
}
