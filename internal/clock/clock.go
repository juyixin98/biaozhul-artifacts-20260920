// Package clock 提供可控时钟，便于测试时间相关逻辑（超时、退避、时间戳）。
package clock

import "time"

// Clock 是时间来源的最小抽象。
type Clock interface {
	Now() time.Time
}

// Real 使用系统墙钟。
type Real struct{}

// Now 返回当前系统时间。
func (Real) Now() time.Time { return time.Now() }

// Fake 是可手动设置的假时钟，并发安全由调用方保证（测试场景单 goroutine 设置）。
type Fake struct {
	t time.Time
}

// NewFake 以给定起点创建假时钟。
func NewFake(start time.Time) *Fake { return &Fake{t: start} }

// Now 返回假时钟当前时间。
func (f *Fake) Now() time.Time { return f.t }

// Advance 将假时钟向前推进 d。
func (f *Fake) Advance(d time.Duration) { f.t = f.t.Add(d) }

// Set 直接设置假时钟时间。
func (f *Fake) Set(t time.Time) { f.t = t }
