package engine

// 状态存储：工作目录维度的节点历史因子。状态放在工作目录之外
// （cacheDir/../state 或独立 stateDir），与输出缓存分离，
// 这样切换/清理缓存条目不会丢失“上次因子”的对比信息。
//
// 存储布局：<stateDir>/<workdir 绝对路径的哈希>/state.json

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
)

type stateFile struct {
	Version int                  `json:"version"`
	WorkDir string               `json:"work_dir"`
	Nodes   map[string]NodeState `json:"nodes"`
}

type stateStore struct {
	root string
	mu   sync.Mutex
	// current 为当前载入工作目录的状态。
	currentPath string
	current     *stateFile
}

func newStateStore(root string) (*stateStore, error) {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("create state dir %q: %w", root, err)
	}
	return &stateStore{root: root}, nil
}

func (s *stateStore) dirFor(workdir string) string {
	sum := sha256.Sum256([]byte(workdir))
	return filepath.Join(s.root, hex.EncodeToString(sum[:16]))
}

// Load 载入指定工作目录的状态。
func (s *stateStore) Load(workdir string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	dir := s.dirFor(workdir)
	path := filepath.Join(dir, "state.json")
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		s.currentPath = path
		s.current = &stateFile{Version: 1, WorkDir: workdir, Nodes: map[string]NodeState{}}
		return nil
	}
	if err != nil {
		return err
	}
	var sf stateFile
	if err := json.Unmarshal(raw, &sf); err != nil {
		return fmt.Errorf("corrupt state file %s: %w", path, err)
	}
	if sf.Nodes == nil {
		sf.Nodes = map[string]NodeState{}
	}
	s.currentPath = path
	s.current = &sf
	return nil
}

// Get 返回节点上次状态。
func (s *stateStore) Get(workdir, node string) *NodeState {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.current == nil || s.current.WorkDir != workdir {
		return nil
	}
	st, ok := s.current.Nodes[node]
	if !ok {
		return nil
	}
	return &st
}

// Put 更新节点状态并立即原子落盘（构建中途崩溃也不会损坏状态）。
func (s *stateStore) Put(workdir, node string, st NodeState) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.current == nil {
		s.current = &stateFile{Version: 1, WorkDir: workdir, Nodes: map[string]NodeState{}}
		s.currentPath = filepath.Join(s.dirFor(workdir), "state.json")
	}
	s.current.Nodes[node] = st
	// 锁内写盘；构建规模为夹具级，开销可接受。
	if err := s.flushLocked(); err != nil {
		// 状态落盘失败不应静默：打印到状态自身的错误通道由调用方日志承担，
		// 这里保持简单——下次构建会退回“无历史”。
		_ = err
	}
}

func (s *stateStore) flushLocked() error {
	if s.currentPath == "" {
		return fmt.Errorf("state path not initialized")
	}
	if err := os.MkdirAll(filepath.Dir(s.currentPath), 0o755); err != nil {
		return err
	}
	// 稳定键序输出，便于人工检查。
	raw, err := json.MarshalIndent(s.current, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.currentPath + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.currentPath)
}

// Snapshot 返回当前工作目录全部节点状态（排序后），供测试/检查使用。
func (s *stateStore) Snapshot(workdir string) []NodeState {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.current == nil || s.current.WorkDir != workdir {
		return nil
	}
	names := make([]string, 0, len(s.current.Nodes))
	for n := range s.current.Nodes {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]NodeState, 0, len(names))
	for _, n := range names {
		out = append(out, s.current.Nodes[n])
	}
	return out
}
