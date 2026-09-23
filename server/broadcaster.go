package server

import (
	"sync"

	"batchagg/batch"
)

// Broadcaster 是一个并发安全的 batch.EventSink，
// 把每个事件扇出给所有当前订阅者（SSE 客户端）。
// 慢订阅者会丢弃事件（每订阅者有固定缓冲，满则丢弃最旧事件），
// 绝不反压调度循环。
type Broadcaster struct {
	mu      sync.Mutex
	clients map[uint64]chan batch.Event
	nextID  uint64
	bufSize int
}

func NewBroadcaster(bufSize int) *Broadcaster {
	if bufSize <= 0 {
		bufSize = 256
	}
	return &Broadcaster{clients: make(map[uint64]chan batch.Event), bufSize: bufSize}
}

// Subscribe 注册一个订阅者，返回事件通道与取消函数。
func (s *Broadcaster) Subscribe() (<-chan batch.Event, func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nextID++
	id := s.nextID
	ch := make(chan batch.Event, s.bufSize)
	s.clients[id] = ch
	return ch, func() {
		s.mu.Lock()
		if c, ok := s.clients[id]; ok {
			delete(s.clients, id)
			close(c)
		}
		s.mu.Unlock()
	}
}

// OnEvent 实现 batch.EventSink。
func (s *Broadcaster) OnEvent(e batch.Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, ch := range s.clients {
		select {
		case ch <- e:
		default:
			// 丢弃最旧事件为最新事件腾位，保持订阅者看到最新状态。
			select {
			case <-ch:
			default:
			}
			select {
			case ch <- e:
			default:
			}
		}
	}
}

// ClientCount 返回当前订阅者数量。
func (s *Broadcaster) ClientCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.clients)
}
