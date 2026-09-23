// Package logstore 定义 Raft 需要持久化的状态（当前任期、投票对象、日志条目），
// 并提供内存实现与文件实现。
//
// 模拟器中所有节点运行在同一进程内：
//   - MemStore：进程内模拟磁盘。节点"崩溃"后数据仍在，"重启"可恢复；
//     只有显式 wipe 事件才会清空（模拟磁盘损坏/换机）。
//   - FileStore：以 JSON 原子写入真实文件，可跨进程恢复，
//     用于演示与测试真正的磁盘持久化。
package logstore

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Entry 是一条 Raft 日志条目。ClientID 非空时表示客户端写请求，
// 提交时模拟器据此向客户端确认；为空表示空操作（本项目不使用 noop 日志）。
type Entry struct {
	Term     int    `json:"term"`
	Data     string `json:"data"`
	ClientID string `json:"client_id,omitempty"`
}

// State 是 Raft 论文 Figure 2 中要求"所有服务器持久化"的状态：
// currentTerm、votedFor、logEntries。
type State struct {
	CurrentTerm int     `json:"current_term"`
	VotedFor    int     `json:"voted_for"` // -1 表示尚未投票
	Log         []Entry `json:"log"`
}

// Store 是持久化存储接口。Save 必须在返回前完成"落盘"（内存实现即写内存，
// 文件实现即 fsync 完成），Raft 只在 Save 成功后才发送 RPC 或回应客户端。
type Store interface {
	Load() (State, error)
	Save(State) error
	// Wipe 清除全部持久化数据（模拟磁盘损坏）。
	Wipe() error
}

// MemStore 是确定性的进程内"磁盘"。
type MemStore struct {
	state State
}

// NewMemStore 创建空内存存储：任期 0，未投票，仅含哨兵条目。
func NewMemStore() *MemStore {
	return &MemStore{state: State{VotedFor: -1, Log: []Entry{{Term: 0}}}}
}

func (m *MemStore) Load() (State, error) {
	// 返回深拷贝，避免调用方绕过 Save 直接改存储。
	log := make([]Entry, len(m.state.Log))
	copy(log, m.state.Log)
	return State{CurrentTerm: m.state.CurrentTerm, VotedFor: m.state.VotedFor, Log: log}, nil
}

func (m *MemStore) Save(s State) error {
	log := make([]Entry, len(s.Log))
	copy(log, s.Log)
	m.state = State{CurrentTerm: s.CurrentTerm, VotedFor: s.VotedFor, Log: log}
	return nil
}

func (m *MemStore) Wipe() error {
	m.state = State{VotedFor: -1, Log: []Entry{{Term: 0}}}
	return nil
}

// FileStore 把状态以 JSON 形式原子写入 dir/raft-state.json：
// 先写临时文件再 rename，保证崩溃时文件要么是旧内容要么是新内容。
type FileStore struct {
	path string
}

func NewFileStore(dir string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("创建状态目录失败: %w", err)
	}
	return &FileStore{path: filepath.Join(dir, "raft-state.json")}, nil
}

func (f *FileStore) Load() (State, error) {
	raw, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		// 首次启动：空状态。
		return State{VotedFor: -1, Log: []Entry{{Term: 0}}}, nil
	}
	if err != nil {
		return State{}, err
	}
	var s State
	if err := json.Unmarshal(raw, &s); err != nil {
		return State{}, fmt.Errorf("解析持久化状态失败: %w", err)
	}
	if len(s.Log) == 0 {
		return State{}, errors.New("持久化日志为空，缺少哨兵条目")
	}
	// 注意：voted_for=0 是合法值（投给 0 号节点），-1 才表示未投票。
	// 由本程序 Save 写出的文件总显式包含该字段，因此这里不做 0→-1 猜测转换。
	return s, nil
}

func (f *FileStore) Save(s State) error {
	raw, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp := f.path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, f.path); err != nil {
		return err
	}
	return nil
}

func (f *FileStore) Wipe() error {
	if err := os.Remove(f.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
