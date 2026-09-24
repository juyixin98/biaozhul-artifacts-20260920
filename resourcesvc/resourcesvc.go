// Package resourcesvc 实现受围栏令牌保护的资源服务（存储）。
//
// 每个资源记录“已见过的最大令牌”：写请求携带的令牌必须 >= 它，
// 否则判定为旧持有者迟到的写并拒绝。等号允许同一持有者重试。
// 状态落盘，进程重启后已见令牌不回退。
package resourcesvc

import (
	"errors"
	"sync"

	"fencingdemo/store"
)

// ErrStaleToken 表示写请求的围栏令牌落后于该资源已见令牌。
var ErrStaleToken = errors.New("stale fencing token: write rejected")

// Resource 是一个受保护资源的当前内容与令牌。
type Resource struct {
	Value string `json:"value"`
	Token uint64 `json:"token"`
}

// Service 是资源服务的核心逻辑，所有方法并发安全。
type Service struct {
	mu      sync.Mutex
	items   map[string]Resource
	stateFn string // 空串表示不持久化（仅限测试）
}

// NewService 创建资源服务；stateDir 为空表示不持久化（仅限测试）。
// 进程重启时传入同一目录即可恢复每个资源的已见令牌。
func NewService(stateDir string) (*Service, error) {
	s := &Service{items: map[string]Resource{}}
	if stateDir != "" {
		s.stateFn = stateDir + "/resourcesvc.json"
		var p struct {
			Resources map[string]Resource `json:"resources"`
		}
		if err := store.Load(s.stateFn, &p); err != nil {
			return nil, err
		}
		if p.Resources != nil {
			s.items = p.Resources
		}
	}
	return s, nil
}

// Write 尝试写入：token 小于该资源已见令牌时返回 ErrStaleToken。
func (s *Service) Write(key, value string, token uint64) error {
	if token == 0 {
		return errors.New("token must be > 0")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, exists := s.items[key]
	if exists && token < cur.Token {
		return ErrStaleToken
	}
	s.items[key] = Resource{Value: value, Token: token}
	return s.persistLocked()
}

// Read 读取资源；不存在时 ok=false。
func (s *Service) Read(key string) (Resource, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.items[key]
	return r, ok
}

func (s *Service) persistLocked() error {
	if s.stateFn == "" {
		return nil
	}
	return store.Save(s.stateFn, &struct {
		Resources map[string]Resource `json:"resources"`
	}{Resources: s.items})
}
