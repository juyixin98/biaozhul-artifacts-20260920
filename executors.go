package agingqueue

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// EchoExecutor 立即成功，把 payload 作为结果返回。类型名："echo"。
type EchoExecutor struct{}

func (EchoExecutor) ExecuteResult(_ context.Context, job *JobHandle) ([]byte, error) {
	if len(job.Payload) == 0 {
		return []byte("ok"), nil
	}
	return job.Payload, nil
}

func (EchoExecutor) Execute(ctx context.Context, job *JobHandle) error {
	_, err := EchoExecutor{}.ExecuteResult(ctx, job)
	return err
}

// SleepExecutor 休眠 payload 指定的时长，尊重 ctx 取消。
// payload 可以是 JSON {"ms": 123} 或纯数字毫秒。类型名："sleep"。
type SleepExecutor struct {
	Clock Clock // 可选；注入 FakeClock 后休眠会随时钟跃迁完成
}

type sleepPayload struct {
	MS int64 `json:"ms"`
}

func (e SleepExecutor) Execute(ctx context.Context, job *JobHandle) error {
	var p sleepPayload
	if len(job.Payload) > 0 {
		if err := json.Unmarshal(job.Payload, &p); err != nil {
			// 兼容纯数字 payload
			var ms int64
			if _, err2 := fmt.Sscanf(string(job.Payload), "%d", &ms); err2 != nil {
				return fmt.Errorf("sleep: invalid payload %q: %w", string(job.Payload), err)
			}
			p.MS = ms
		}
	}
	d := time.Duration(p.MS) * time.Millisecond
	if d <= 0 {
		return nil
	}
	if e.Clock != nil {
		return e.Clock.Sleep(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// FlakyExecutor 前 FailTimes 次尝试失败，之后成功，用于演示重试。
// 类型名："flaky"。payload 可覆盖：{"fail_times": 2, "message": "..."}。
type FlakyExecutor struct {
	FailTimes int      // 默认 1
	counters  sync.Map // jobID -> *int64（仅同 ID 重试点才有意义；按 attempt 更可靠）
}

type flakyPayload struct {
	FailTimes int    `json:"fail_times"`
	Message   string `json:"message"`
}

func (f *FlakyExecutor) Execute(_ context.Context, job *JobHandle) error {
	var p flakyPayload
	_ = json.Unmarshal(job.Payload, &p)
	fail := f.FailTimes
	if fail <= 0 {
		fail = 1
	}
	if p.FailTimes > 0 {
		fail = p.FailTimes
	}
	v, _ := f.counters.LoadOrStore(job.ID, new(int64))
	n := atomic.AddInt64(v.(*int64), 1)
	if int(n) <= fail {
		msg := p.Message
		if msg == "" {
			msg = "simulated failure"
		}
		return fmt.Errorf("%s (attempt %d)", msg, job.Attempt)
	}
	return nil
}

// ErrGateClosed 由 GateExecutor 在关闭状态下返回。
var ErrGateClosed = errors.New("agingqueue: gate executor blocked")

// GateExecutor 是测试执行器：阻塞直到 Open()/Release() 或 ctx 取消。
type GateExecutor struct {
	gate chan struct{}
}

func NewGateExecutor() *GateExecutor {
	return &GateExecutor{gate: make(chan struct{})}
}

// Release 放行所有当前和未来的 Execute 调用（直到再次 Close）。
func (g *GateExecutor) Release() {
	select {
	case <-g.gate:
		// 已 open，保持 open
	default:
		close(g.gate)
	}
}

// CloseGate 重新关闭（之后的 Execute 再次阻塞）。
func (g *GateExecutor) CloseGate() {
	select {
	case <-g.gate:
		g.gate = make(chan struct{})
	default:
	}
}

func (g *GateExecutor) Execute(ctx context.Context, job *JobHandle) error {
	select {
	case <-g.gate:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// RegisterBuiltinExecutors 注册 echo / sleep / flaky 三个内置执行器。
func RegisterBuiltinExecutors(s *Scheduler, clock Clock) {
	s.RegisterExecutor("echo", EchoExecutor{})
	s.RegisterExecutor("sleep", SleepExecutor{Clock: clock})
	s.RegisterExecutor("flaky", &FlakyExecutor{FailTimes: 1})
}
