package service

import (
	"sync"
)

// holdState 描述一次占位执行的结局。
type holdState int

const (
	holdOpen      holdState = iota // 仍在执行
	holdCommitted                  // 已提交（结果可重放）
	holdReleased                   // 已释放（可自行重试）
)

// Hold 是"处理中重复请求"的等待点：首个执行者占位时创建，
// 重复请求在此阻塞等待结局，拿到明确响应（重放结果 或 可重试信号），
// 而不是收到模糊的错误后盲目重试。
type Hold struct {
	done  chan struct{}
	mu    sync.Mutex
	state holdState
}

func newHold() *Hold {
	return &Hold{done: make(chan struct{}), state: holdOpen}
}

// doneChan 返回结局通道（关闭即代表执行结束）。
func (h *Hold) doneChan() <-chan struct{} { return h.done }

// resolve 记录结局并唤醒所有等待者，幂等且只生效一次。
func (h *Hold) resolve(state holdState) {
	h.mu.Lock()
	if h.state != holdOpen {
		h.mu.Unlock()
		return
	}
	h.state = state
	close(h.done)
	h.mu.Unlock()
}

func (h *Hold) doneCommitted() { h.resolve(holdCommitted) }
func (h *Hold) doneReleased()  { h.resolve(holdReleased) }

// State 返回当前结局。
func (h *Hold) State() holdState {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.state
}
