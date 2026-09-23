// Package wal 提供节点级的只追加预写日志（write-ahead log）。
//
// 模拟真实 2PC 中“崩溃后仍可恢复”的持久化存储：
//   - 每条记录以一行 JSON + '\n' 追加写入，写入后立即 fsync（模拟同步刷盘）；
//   - 崩溃不会丢失已 fsync 的记录；
//   - 重启时按顺序重放整个文件；
//   - 同一文件可在多次 Run 之间复用（CLI 的 resume 场景），此时只能追加新记录。
package wal

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Record 是 WAL 中的一条日志记录。Type 决定 Data 的 JSON 形态。
type Record struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data"`
}

// WAL 是一个已打开的追加日志句柄。
type WAL struct {
	nodeID string
	path   string
	f      *os.File
	w      *bufio.Writer
	total  int // 磁盘上记录总数（含历史进程留下的）
}

// Open 打开（或创建）nodeID 对应的日志文件。
// 若文件已有内容（跨进程 resume），旧记录保留，后续只允许追加。
func Open(dir, nodeID string) (*WAL, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	p := filepath.Join(dir, nodeID+".wal")
	f, err := os.OpenFile(p, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	w := &WAL{nodeID: nodeID, path: p, f: f, w: bufio.NewWriter(f)}
	if err := w.Replay(func(Record) error { w.total++; return nil }); err != nil {
		f.Close()
		return nil, err
	}
	return w, nil
}

// Append 原子地追加一条记录并同步刷盘。
func (w *WAL) Append(recType string, payload any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	line, err := json.Marshal(Record{Type: recType, Data: data})
	if err != nil {
		return err
	}
	if _, err := w.w.Write(line); err != nil {
		return err
	}
	if err := w.w.WriteByte('\n'); err != nil {
		return err
	}
	if err := w.w.Flush(); err != nil {
		return err
	}
	// 模拟真实数据库在状态转移前的 fsync。
	if err := w.f.Sync(); err != nil {
		return err
	}
	w.total++
	return nil
}

// Total 返回磁盘上的记录总数（含历史记录）。
func (w *WAL) Total() int { return w.total }

// Replay 按写入顺序重放磁盘上的全部记录（包括历史进程留下的）。
func (w *WAL) Replay(fn func(Record) error) error {
	f, err := os.Open(w.path)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec Record
		if err := json.Unmarshal(line, &rec); err != nil {
			return fmt.Errorf("wal %s 第 %d 行损坏: %w", w.nodeID, lineNo, err)
		}
		if err := fn(rec); err != nil {
			return err
		}
	}
	return sc.Err()
}

// Close 关闭日志文件。
func (w *WAL) Close() error {
	if err := w.w.Flush(); err != nil {
		return err
	}
	return w.f.Close()
}

// Path 返回日志文件路径。
func (w *WAL) Path() string { return w.path }
