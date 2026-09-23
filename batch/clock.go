package batch

import "time"

// Clock 是调度器使用的时间源。生产环境使用 SystemClock，
// 测试使用可手动推进的 FakeClock，从而确定性地验证等待超时等行为。
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Timer 与 time.Timer 的接口子集一致，便于用虚拟时钟替换。
type Timer interface {
	C() <-chan time.Time
	Stop() bool
	Reset(d time.Duration) bool
}

// SystemClock 基于真实墙钟时间。
type SystemClock struct{}

func (SystemClock) Now() time.Time { return time.Now() }

func (SystemClock) NewTimer(d time.Duration) Timer {
	return &systemTimer{t: time.NewTimer(d)}
}

type systemTimer struct{ t *time.Timer }

func (t *systemTimer) C() <-chan time.Time { return t.t.C }
func (t *systemTimer) Stop() bool          { return t.t.Stop() }
func (t *systemTimer) Reset(d time.Duration) bool {
	return t.t.Reset(d)
}
