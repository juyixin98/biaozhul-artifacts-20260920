package agingqueue

import (
	"context"
	"errors"
	"time"
)

// State 是作业的生命周期状态。
type State string

const (
	StateQueued          State = "queued"           // 在就绪堆中等待派发
	StateDelayed         State = "delayed"          // 重试退避中
	StateRunning         State = "running"          // 正在执行
	StateSucceeded       State = "succeeded"        // 终态
	StateCanceled        State = "canceled"         // 终态
	StateCancelRequested State = "cancel_requested" // 运行中，执行器被要求停止
	StateFailed          State = "failed"           // 终态：重试次数耗尽
)

func (s State) Terminal() bool {
	switch s {
	case StateSucceeded, StateCanceled, StateFailed:
		return true
	}
	return false
}

// MinPriority / MaxPriority 是离散优先级的闭区间边界；数字越大越紧急。
const (
	MinPriority = 0
	MaxPriority = 9
)

// 取消相关错误。HTTP 层据此映射状态码，测试据此断言重复取消。
var (
	ErrNotFound        = errors.New("agingqueue: job not found")
	ErrAlreadyCanceled = errors.New("agingqueue: job already canceled")
	ErrTerminalCancel  = errors.New("agingqueue: job already in terminal state")
)

// Executor 是可替换的作业执行器。Scheduler 对每个作业按名字查找执行器。
type Executor interface {
	Execute(ctx context.Context, job *JobHandle) error
}

// JobHandle 是传给执行器的作业句柄，暴露只读元数据与载荷。
type JobHandle struct {
	ID         string
	Type       string
	Priority   int // 基础优先级（非老化后的）
	Attempt    int // 当前尝试，1 表示首次执行
	Payload    []byte
	EnqueuedAt time.Time // 首次入队时刻（重试不改变）
}

// Job 是调度器内部的作业记录（不导出；外部通过 JobView 观察）。
type Job struct {
	id       string
	typ      string
	priority int // 基础优先级
	payload  []byte

	maxAttempts int
	backoff     time.Duration
	backoffMax  time.Duration
	attempts    int

	state State

	// 年龄锚点，作业整个生命周期不变（含重试）。这保证"重试不允许通过
	// 重置年龄长期插队"：重新入队时 seq 与 enqueuedAt 均沿用首次入队值。
	enqueuedAt time.Time
	seq        int64

	// accWait 是历史上在就绪队列中累计的等待时长；readySince 是本轮
	// 进入就绪队列的时刻。退避期间不累计等待。
	accWait    time.Duration
	readySince time.Time

	// 老化后缓存的当前有效优先级，以及下一次跳档时刻（仅 queued 时有效）。
	curPriority int
	nextBoostAt time.Time

	// readyHeap 中的位置；不在就绪堆时为 -1。
	heapIdx int
	// 三个堆的反向引用，供取消时精确移除。
	delayedRef *delayedEntry
	boostRef   *boostEntry
	cancelFn   context.CancelFunc

	// 运行时信息
	startedAt  time.Time
	finishedAt time.Time
	lastErr    string
	result     []byte
}

// effectivePriority 计算某一时刻的有效优先级：
// 有效优先级 = min(MaxPriority, base + floor(总就绪等待 / agingStep))。
// 只有在就绪队列里的等待计入年龄；运行中、退避中不计。
func (j *Job) effectivePriority(now time.Time, agingStep time.Duration) int {
	wait := j.accWait
	if j.state == StateQueued && !j.readySince.IsZero() {
		wait += now.Sub(j.readySince)
	}
	return clampPriority(j.priority + int(wait/agingStep))
}

func clampPriority(p int) int {
	if p < MinPriority {
		return MinPriority
	}
	if p > MaxPriority {
		return MaxPriority
	}
	return p
}

// JobView 是作业的对外只读快照。
type JobView struct {
	ID                string    `json:"id"`
	Seq               int64     `json:"seq"` // 首次入队序号，同优先级 FIFO 依据
	Type              string    `json:"type"`
	Priority          int       `json:"priority"`           // 基础优先级
	EffectivePriority int       `json:"effective_priority"` // 当前（老化后）有效优先级
	State             State     `json:"state"`
	Attempts          int       `json:"attempts"`
	MaxAttempts       int       `json:"max_attempts"`
	EnqueuedAt        time.Time `json:"enqueued_at"`
	ReadySince        time.Time `json:"ready_since,omitempty"`
	StartedAt         time.Time `json:"started_at,omitempty"`
	FinishedAt        time.Time `json:"finished_at,omitempty"`
	LastError         string    `json:"last_error,omitempty"`
	NextRetryAt       time.Time `json:"next_retry_at,omitempty"`
	Result            string    `json:"result,omitempty"`
}

// SubmitOptions 是单次入队参数。
type SubmitOptions struct {
	Type        string
	Priority    int
	Payload     []byte
	MaxAttempts int           // <=0 用调度器默认值
	Backoff     time.Duration // 0 用调度器默认值
	ID          string        // 可选自定义 ID；空则自动生成
}
