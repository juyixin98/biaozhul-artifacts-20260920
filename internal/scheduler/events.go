package scheduler

import (
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"

	"resourcebooking/internal/clock"
)

// 事件类型常量。事件是状态变更的事实记录：先完成内部状态
// 变更，再发出对应事件；事件只追加、不修改、不删除。
const (
	// EventResourceAdded 新资源注册。
	EventResourceAdded = "resource.added"
	// EventReservationCreated 单条预约创建成功。
	EventReservationCreated = "reservation.created"
	// EventReservationRejected 单条预约请求被拒绝（未落地）。
	EventReservationRejected = "reservation.rejected"
	// EventReservationBatchCreated 批量预约整体成功。
	EventReservationBatchCreated = "reservation.batch_created"
	// EventReservationBatchRejected 批量预约整体拒绝（无任何状态变更）。
	EventReservationBatchRejected = "reservation.batch_rejected"
	// EventReservationCancelled 预约被取消。
	EventReservationCancelled = "reservation.cancelled"
	// EventReservationStarted 预约时段开始（pending -> running）。
	EventReservationStarted = "reservation.started"
	// EventReservationCompleted 预约时段正常结束。
	EventReservationCompleted = "reservation.completed"
	// EventReservationFailed 执行器报告失败，预约提前终止。
	EventReservationFailed = "reservation.failed"
)

// Event 是一条结构化的状态变更记录。
type Event struct {
	// Seq 为进程内单调递增的事件序号（从 1 开始）。
	Seq int64 `json:"seq"`
	// At 为产生事件时的墙钟时间（来自可替换 Clock）。
	At time.Time `json:"at"`
	// Type 为事件类型（见 Event* 常量）。
	Type string `json:"type"`
	// Detail 为随事件类型变化的结构化负载。
	Detail any `json:"detail"`
}

// BatchCreatedDetail 是批量创建成功事件的负载。
type BatchCreatedDetail struct {
	IDs   []string       `json:"ids"`
	Count int            `json:"count"`
	Items []*Reservation `json:"items"`
}

// BatchRejectedDetail 是批量拒绝事件的负载。
type BatchRejectedDetail struct {
	Items []RejectItemError `json:"items"`
}

// RejectedDetail 是单条预约拒绝事件的负载。
type RejectedDetail struct {
	RequestID string `json:"request_id,omitempty"`
	Resource  string `json:"resource,omitempty"`
	Error     *Error `json:"error"`
}

// CancelledDetail 是取消事件的负载。
type CancelledDetail struct {
	ID       string `json:"id"`
	Resource string `json:"resource"`
	At       Ticks  `json:"at"`
}

// StartedDetail 是开始/完成/失败事件的负载。
type StartedDetail struct {
	ID       string   `json:"id"`
	Resource string   `json:"resource"`
	Interval Interval `json:"interval"`
}

// FailedDetail 在 StartedDetail 基础上附带失败原因。
type FailedDetail struct {
	StartedDetail
	Reason string `json:"reason"`
}

// Sink 接收事件的外部落点（例如 JSON Lines 文件）。
type Sink interface {
	Write(Event) error
	io.Closer
}

// EventLog 是线程安全的内存事件日志，支持重放、订阅与外部 Sink。
type EventLog struct {
	clk   clock.Clock
	mu    sync.Mutex
	seq   int64
	store []Event
	subs  map[int64]chan Event
	subID int64
	sinks []Sink
}

// NewEventLog 创建事件日志。clk 为 nil 时使用系统墙钟。
func NewEventLog(clk clock.Clock) *EventLog {
	if clk == nil {
		clk = clock.Wall{}
	}
	return &EventLog{clk: clk, subs: map[int64]chan Event{}}
}

// AddSink 追加一个外部事件落点；之后的每条事件都会同步写入。
func (l *EventLog) AddSink(s Sink) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.sinks = append(l.sinks, s)
}

// Emit 构造并记录一条事件，返回最终记录（含序号与时间戳）。
func (l *EventLog) Emit(typ string, detail any) Event {
	l.mu.Lock()
	l.seq++
	e := Event{Seq: l.seq, At: l.clk.Now(), Type: typ, Detail: detail}
	l.store = append(l.store, e)
	sinks := append([]Sink(nil), l.sinks...)
	subs := make([]chan Event, 0, len(l.subs))
	for _, ch := range l.subs {
		subs = append(subs, ch)
	}
	l.mu.Unlock()

	// Sink 写入与订阅推送都在锁外执行，避免慢消费者阻塞调度。
	for _, s := range sinks {
		// Sink 失败只影响外部持久化，不影响内存状态；忽略错误
		// 但写入调用方提供的 io.Writer 时通常不会失败。
		_ = s.Write(e)
	}
	for _, ch := range subs {
		select {
		case ch <- e:
		default:
			// 订阅者缓冲已满则丢弃该订阅者的本条事件，
			// 不阻塞调度主路径。
		}
	}
	return e
}

// Events 返回事件的快照副本。
func (l *EventLog) Events() []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Event, len(l.store))
	copy(out, l.store)
	return out
}

// Since 返回序号严格大于 afterSeq 的事件快照。
func (l *EventLog) Since(afterSeq int64) []Event {
	all := l.Events()
	idx := 0
	for idx < len(all) && all[idx].Seq <= afterSeq {
		idx++
	}
	return all[idx:]
}

// Subscribe 注册一个带缓冲的实时事件订阅，返回订阅 ID 与事件通道。
// 取消订阅必须调用 Unsubscribe。
func (l *EventLog) Subscribe(buffer int) (int64, <-chan Event) {
	if buffer < 1 {
		buffer = 1
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.subID++
	id := l.subID
	ch := make(chan Event, buffer)
	l.subs[id] = ch
	return id, ch
}

// Unsubscribe 取消订阅并关闭对应通道。
func (l *EventLog) Unsubscribe(id int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if ch, ok := l.subs[id]; ok {
		delete(l.subs, id)
		close(ch)
	}
}

// Close 关闭所有外部 Sink。
func (l *EventLog) Close() error {
	l.mu.Lock()
	sinks := l.sinks
	l.sinks = nil
	l.mu.Unlock()
	var firstErr error
	for _, s := range sinks {
		if err := s.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// JSONLSink 把事件以 JSON Lines 格式写入任意 io.Writer（如文件）。
type JSONLSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONLSink 创建 JSON Lines 事件落点。
func NewJSONLSink(w io.Writer) *JSONLSink { return &JSONLSink{w: w} }

// Write 实现 Sink。
func (s *JSONLSink) Write(e Event) error {
	data, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("序列化事件: %w", err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := s.w.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("写入事件: %w", err)
	}
	return nil
}

// Close 实现 Sink；不关闭底层 Writer（由打开者负责）。
func (s *JSONLSink) Close() error { return nil }
