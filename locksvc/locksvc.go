// Package locksvc 实现带围栏令牌（fencing token）的锁服务。
//
// 每次把锁授予新持有者时，服务发出一个严格递增的令牌；
// 资源服务（见 resourcesvc）用它识别“旧持有者迟到的写”。
// 令牌计数与租约落盘，进程重启后计数不回退。
package locksvc

import (
	"errors"
	"sync"
	"time"

	"fencingdemo/clock"
	"fencingdemo/store"
)

var (
	// ErrConflict 表示锁仍被其他有效持有者占用。
	ErrConflict = errors.New("lock held by another holder")
	// ErrNotHeld 表示租约不存在、已过期，或持有者不匹配。
	ErrNotHeld = errors.New("no active lease for holder")
	// ErrInvalidTTL 表示 ttl_ms 缺失或非正数。
	ErrInvalidTTL = errors.New("ttl_ms must be > 0")
)

// Lease 是一把锁当前（或最近一次）的租约状态。
type Lease struct {
	Holder    string    `json:"holder"`
	Token     uint64    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

// Active 判断租约在 t 时刻是否仍未过期。
func (l *Lease) Active(t time.Time) bool { return t.Before(l.ExpiresAt) }

type persisted struct {
	Counter uint64           `json:"counter"`
	Leases  map[string]Lease `json:"leases"`
}

// Service 是锁服务的核心逻辑，所有方法并发安全。
type Service struct {
	mu      sync.Mutex
	clk     clock.Clock
	state   persisted
	stateFn string // 空串表示不持久化（仅限测试）
}

// NewService 创建锁服务；stateDir 为空表示不持久化（仅限测试）。
// 进程重启时传入同一目录即可恢复令牌计数，保证令牌不回退。
func NewService(clk clock.Clock, stateDir string) (*Service, error) {
	s := &Service{clk: clk, state: persisted{Leases: map[string]Lease{}}}
	if stateDir != "" {
		s.stateFn = stateDir + "/locksvc.json"
		if err := store.Load(s.stateFn, &s.state); err != nil {
			return nil, err
		}
		if s.state.Leases == nil {
			s.state.Leases = map[string]Lease{}
		}
	}
	return s, nil
}

// Acquire 尝试获取名为 name 的锁。
//   - 锁空闲/已过期：counter 加一并把新令牌授予 holder；
//   - 当前持有者就是 holder：续租，令牌不变；
//   - 当前持有者是其他人且租约有效：返回 ErrConflict。
func (s *Service) Acquire(name, holder string, ttl time.Duration) (Lease, error) {
	if ttl <= 0 {
		return Lease{}, ErrInvalidTTL
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.clk.Now()
	if cur, ok := s.state.Leases[name]; ok && cur.Active(now) {
		if cur.Holder != holder {
			return cur, ErrConflict
		}
		// 同一持有者续租：保留原围栏令牌，只延长过期时间。
		cur.ExpiresAt = now.Add(ttl)
		s.state.Leases[name] = cur
		if err := s.persistLocked(); err != nil {
			return Lease{}, err
		}
		return cur, nil
	}

	s.state.Counter++
	l := Lease{Holder: holder, Token: s.state.Counter, ExpiresAt: now.Add(ttl)}
	s.state.Leases[name] = l
	if err := s.persistLocked(); err != nil {
		return Lease{}, err
	}
	return l, nil
}

// Release 由 holder 主动释放锁；其他人或过期租约释放返回 ErrNotHeld。
func (s *Service) Release(name, holder string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, ok := s.state.Leases[name]
	if !ok || !cur.Active(s.clk.Now()) || cur.Holder != holder {
		return ErrNotHeld
	}
	delete(s.state.Leases, name)
	return s.persistLocked()
}

// Lease 返回当前有效租约；不存在或已过期时 ok=false。
func (s *Service) Lease(name string) (Lease, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, ok := s.state.Leases[name]
	if !ok || !cur.Active(s.clk.Now()) {
		return Lease{}, false
	}
	return cur, true
}

// Counter 返回已发出的最大围栏令牌（用于测试/检查重启后不回退）。
func (s *Service) Counter() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state.Counter
}

func (s *Service) persistLocked() error {
	if s.stateFn == "" {
		return nil
	}
	return store.Save(s.stateFn, &s.state)
}
