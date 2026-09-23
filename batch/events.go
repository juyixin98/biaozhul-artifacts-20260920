package batch

import "time"

// 事件类型常量。状态转换均以结构化事件记录，可用于断言、指标与审计。
const (
	// EventItemRejected：单项超过 MaxItemBytes，在进入批之前被拒绝。
	EventItemRejected = "item.rejected"
	// EventBatchOpened：某个合批键创建了新批并纳入第一个请求。
	EventBatchOpened = "batch.opened"
	// EventItemAdmitted：请求被纳入一个打开的批。
	EventItemAdmitted = "item.admitted"
	// EventItemCanceled：请求在被执行前取消。
	EventItemCanceled = "item.canceled"
	// EventBatchFlushed：批被关闭并提交给执行器。
	EventBatchFlushed = "batch.flushed"
	// EventItemSucceeded：批中的单项执行成功。
	EventItemSucceeded = "item.succeeded"
	// EventItemFailed：批中的单项执行失败（部分失败）。
	EventItemFailed = "item.failed"
	// EventBatchCompleted：整批执行结束。
	EventBatchCompleted = "batch.completed"
	// EventDrainStarted：Shutdown 被调用，不再接受新请求。
	EventDrainStarted = "drain.started"
	// EventDrainCompleted：所有在途批处理完毕，调度器完全停止。
	EventDrainCompleted = "drain.completed"
)

// 发批触发原因。
const (
	ReasonCount   = "max_count"
	ReasonBytes   = "max_bytes"
	ReasonTimeout = "max_wait"
	ReasonDrain   = "drain"
)

// Event 描述一次调度状态变更。所有字段均为自描述的纯数据，
// 可直接 JSON 序列化输出到日志或 SSE。
type Event struct {
	Type      string            `json:"type"`
	At        time.Time         `json:"at"`
	Key       string            `json:"key,omitempty"`
	BatchID   string            `json:"batch_id,omitempty"`
	RequestID string            `json:"request_id,omitempty"`
	Reason    string            `json:"reason,omitempty"`
	Count     int               `json:"count,omitempty"`
	Bytes     int               `json:"bytes,omitempty"`
	Err       string            `json:"error,omitempty"`
	Meta      map[string]string `json:"meta,omitempty"`
}

// EventSink 接收调度事件。实现必须保证并发安全。
type EventSink interface {
	OnEvent(Event)
}

// EventSinkFunc 把普通函数适配为 EventSink。
type EventSinkFunc func(Event)

func (f EventSinkFunc) OnEvent(e Event) { f(e) }

// NopSink 丢弃全部事件，为默认接收器。
type NopSink struct{}

func (NopSink) OnEvent(Event) {}

// ChannelSink 把事件写入缓冲通道，供测试同步使用。
// 通道满时丢弃事件，保证调度主循环永不被慢消费者阻塞。
type ChannelSink struct {
	ch chan Event
}

func NewChannelSink(buf int) *ChannelSink {
	return &ChannelSink{ch: make(chan Event, buf)}
}

func (s *ChannelSink) OnEvent(e Event) {
	select {
	case s.ch <- e:
	default:
	}
}

// Events 返回底层事件通道。
func (s *ChannelSink) Events() <-chan Event { return s.ch }

// MultiSink 把同一事件扇出到多个接收器。
type MultiSink []EventSink

func (m MultiSink) OnEvent(e Event) {
	for _, s := range m {
		s.OnEvent(e)
	}
}
