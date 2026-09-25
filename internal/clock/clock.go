// Package clock 定义可替换的调度时钟。
//
// 调度库本身只使用抽象的整数 tick（见 scheduler.Ticks），
// 但事件记录与 HTTP 层需要墙钟时间。生产环境使用 Wall，
// 测试环境使用可手动设置/推进的 Fake。
package clock

import (
	"sync"
	"time"
)

// Clock 是调度器观察时间的唯一入口。
type Clock interface {
	Now() time.Time
}

// Wall 返回系统墙钟时间（UTC）。
type Wall struct{}

// Now 实现 Clock。
func (Wall) Now() time.Time { return time.Now().UTC() }

// Fake 是可手动控制的时钟，供测试与离线回放使用。
type Fake struct {
	mu sync.RWMutex
	t  time.Time
}

// NewFake 以给定时刻创建一个假时钟（内部统一转为 UTC）。
func NewFake(t time.Time) *Fake {
	return &Fake{t: t.UTC()}
}

// Now 实现 Clock。
func (f *Fake) Now() time.Time {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.t
}

// Set 直接设置当前时刻。
func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = t.UTC()
}

// Advance 将时钟向前推进 d。
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}
