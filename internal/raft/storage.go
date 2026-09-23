package raft

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// PersistentState 是 Raft 必须在响应前落盘的全部状态。
type PersistentState struct {
	CurrentTerm int     `json:"currentTerm"`
	VotedFor    int     `json:"votedFor"`
	Log         []Entry `json:"log"`
}

// Storage 抽象节点的持久化存储，使得“崩溃-重启”可以从磁盘恢复，
// 也便于测试时替换为内存实现。
type Storage interface {
	Load() (*PersistentState, error)
	Save(PersistentState) error
	// Reset 清除全部持久化状态（模拟器初始化一个全新集群时使用）。
	Reset() error
}

// FileStorage 把状态以 JSON 写入 <dir>/state.json，
// 采用“临时文件 + rename”保证单次保存的原子性。
type FileStorage struct {
	path string
}

func NewFileStorage(dir string) (*FileStorage, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create state dir %q: %w", dir, err)
	}
	return &FileStorage{path: filepath.Join(dir, "state.json")}, nil
}

func (s *FileStorage) Load() (*PersistentState, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var ps PersistentState
	if err := json.Unmarshal(data, &ps); err != nil {
		return nil, fmt.Errorf("corrupt state file %q: %w", s.path, err)
	}
	return &ps, nil
}

func (s *FileStorage) Save(ps PersistentState) error {
	data, err := json.MarshalIndent(ps, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *FileStorage) Reset() error {
	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Remove(s.path + ".tmp"); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
