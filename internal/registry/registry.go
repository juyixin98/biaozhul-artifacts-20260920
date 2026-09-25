// Package registry 是一个进程内假契约仓库，充当“外部依赖”的替身：
// 契约按名称+版本存取，绝不连接任何生产系统。
package registry

import (
	"fmt"
	"sort"
	"sync"
)

// Contract 是一份带名称与版本的契约（内容为 JSON Schema 原文）。
type Contract struct {
	Name    string         `json:"name"`
	Version string         `json:"version"`
	Schema  map[string]any `json:"schema"`
}

// Store 是线程安全的内存契约仓库。
type Store struct {
	mu        sync.RWMutex
	contracts map[string]map[string]Contract // name -> version -> contract
}

// New 创建空仓库。
func New() *Store {
	return &Store{contracts: map[string]map[string]Contract{}}
}

// Put 存入或覆盖一份契约。
func (s *Store) Put(c Contract) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.contracts[c.Name] == nil {
		s.contracts[c.Name] = map[string]Contract{}
	}
	s.contracts[c.Name][c.Version] = c
}

// Get 取出一份契约。
func (s *Store) Get(name, version string) (Contract, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	versions, ok := s.contracts[name]
	if !ok {
		return Contract{}, fmt.Errorf("契约 %q 不存在", name)
	}
	c, ok := versions[version]
	if !ok {
		return Contract{}, fmt.Errorf("契约 %q 没有版本 %q", name, version)
	}
	return c, nil
}

// List 列出所有契约名称与版本，按字典序排序，保证输出确定。
func (s *Store) List() []Contract {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []Contract
	for _, versions := range s.contracts {
		for _, c := range versions {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Name != out[j].Name {
			return out[i].Name < out[j].Name
		}
		return out[i].Version < out[j].Version
	})
	return out
}
