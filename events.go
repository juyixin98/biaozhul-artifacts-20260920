package agingqueue

import (
	"sync"
	"time"
)

// EventType 是作业生命周期事件的种类。
type EventType string

const (
	EventSubmitted       EventType = "submitted"        // 作业入队
	EventReady           EventType = "ready"            // 退避结束回到就绪队列
	EventPriorityBoost   EventType = "priority_boosted" // 等待老化使有效优先级跳升一档
	EventStarted         EventType = "started"          // 开始执行
	EventSucceeded       EventType = "succeeded"        // 执行成功（终态）
	EventFailed          EventType = "failed"           // 本次尝试失败，将重试
	EventRetryScheduled  EventType = "retry_scheduled"  // 已安排退避后的重试
	EventCancelRequested EventType = "cancel_requested" // 取消请求被接受
	EventCanceled        EventType = "canceled"         // 作业进入终态 canceled
	EventExhausted       EventType = "exhausted"        // 重试次数耗尽（终态）
)

// Event 是不可变的状态变更记录。所有字段导出，可直接 JSON 序列化。
type Event struct {
	Seq        int64          `json:"seq"` // 事件全局单调序号
	At         time.Time      `json:"at"`  // 事件发生时刻（取自调度器时钟）
	Type       EventType      `json:"type"`
	JobID      string         `json:"job_id"`
	Priority   int            `json:"priority,omitempty"` // 当前有效优先级（如适用）
	BasePri    int            `json:"base_priority,omitempty"`
	Attempt    int            `json:"attempt,omitempty"` // 当前尝试次数（从 1 开始）
	MaxAttempt int            `json:"max_attempts,omitempty"`
	Err        string         `json:"error,omitempty"`
	Detail     map[string]any `json:"detail,omitempty"` // 附加结构化信息
}

// EventLog 线程安全地保存最近的结构化事件，并支持订阅实时事件流。
type EventLog struct {
	mu        sync.Mutex
	seq       int64
	events    []Event
	capacity  int
	subs      map[chan Event]struct{}
	subBufLen int
}

// NewEventLog 创建环形容量为 capacity 的事件日志。capacity <= 0 表示不限制。
func NewEventLog(capacity int) *EventLog {
	return &EventLog{
		capacity:  capacity,
		events:    make([]Event, 0, min(max(capacity, 0), 1024)),
		subs:      make(map[chan Event]struct{}),
		subBufLen: 64,
	}
}

func (l *EventLog) emit(at time.Time, t EventType, j *Job, detail map[string]any) Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.seq++
	e := Event{
		Seq:        l.seq,
		At:         at,
		Type:       t,
		JobID:      j.id,
		Priority:   j.curPriority,
		BasePri:    j.priority,
		Attempt:    j.attempts,
		MaxAttempt: j.maxAttempts,
		Detail:     detail,
	}
	if j.lastErr != "" {
		e.Err = j.lastErr
	}
	l.events = append(l.events, e)
	if l.capacity > 0 && len(l.events) > l.capacity {
		l.events = l.events[len(l.events)-l.capacity:]
	}
	// 发送与 Unsubscribe 在同一把锁内串行，杜绝"发送中关闭通道"的竞争。
	// 订阅方缓冲满时丢弃该事件（实时流仅用于观察，历史可查 Events）。
	for ch := range l.subs {
		select {
		case ch <- e:
		default:
		}
	}
	return e
}

// Events 返回事件快照（早于 beforeSeq 的事件；beforeSeq <= 0 表示最新）。
// limit <= 0 时返回全部匹配事件。
func (l *EventLog) Events(beforeSeq int64, limit int) []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	src := l.events
	if beforeSeq > 0 {
		idx := len(src)
		for idx > 0 && src[idx-1].Seq >= beforeSeq {
			idx--
		}
		src = src[:idx]
	}
	if limit > 0 && len(src) > limit {
		src = src[len(src)-limit:]
	}
	out := make([]Event, len(src))
	copy(out, src)
	return out
}

// Subscribe 订阅实时事件流。返回的通道在 Unsubscribe 或缓冲满丢弃时仍安全。
func (l *EventLog) Subscribe() (chan Event, int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	ch := make(chan Event, l.subBufLen)
	l.subs[ch] = struct{}{}
	return ch, l.seq
}

func (l *EventLog) Unsubscribe(ch chan Event) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.subs[ch]; ok {
		delete(l.subs, ch)
		close(ch)
	}
}

// EventSubscribers 返回当前订阅者数量（供指标/测试使用）。
func (l *EventLog) SubscriberCount() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.subs)
}
