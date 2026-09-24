// Package clock 提供可注入的时钟抽象：
// 生产环境使用 Real，测试使用 Fake 以便确定性推进时间。
package clock

import (
	"sync"
	"time"
)

// Clock 是时间来源的抽象，锁租约过期判断只依赖它。
type Clock interface {
	Now() time.Time
}

// Real 使用系统时间。
type Real struct{}

// Now 返回当前系统时间。
func (Real) Now() time.Time { return time.Now() }

// Fake 是测试用手动时钟，只能通过 Advance 前进，保证单调。
type Fake struct {
	mu sync.Mutex
	t  time.Time
}

// NewFake 创建从 t 开始的手动时钟。
func NewFake(t time.Time) *Fake { return &Fake{t: t} }

// Now 返回手动时钟当前时间。
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

// Advance 将手动时钟推进 d。
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}
