// Package wal 是极简的追加式 JSONL 持久化日志，仅用于本地样例场景。
// 每条摄入请求落成一行 JSON；启动时按行重放重建内存状态。
// 不提供压缩、分段或快照（样例项目刻意保持简单，README 中已说明局限）。
package wal

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"counterreset/internal/series"
)

// Record 是日志中的一条记录：某条序列上的一批样本。
type Record struct {
	Labels  map[string]string `json:"labels"`
	Samples []series.Sample   `json:"samples"`
}

// WAL 包装一个追加文件。所有写入串行化并 fsync（样例数据量小，优先可预期性）。
type WAL struct {
	mu sync.Mutex
	f  *os.File
	w  *bufio.Writer
}

// Open 打开（不存在则创建）WAL；目录会自动创建。
func Open(path string) (*WAL, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("创建 WAL 目录失败: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("打开 WAL 失败: %w", err)
	}
	return &WAL{f: f, w: bufio.NewWriterSize(f, 4096)}, nil
}

// Append 追加一条记录并 fsync。
func (l *WAL) Append(rec Record) error {
	if err := validateRecord(rec); err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	enc := json.NewEncoder(l.w)
	if err := enc.Encode(rec); err != nil {
		return fmt.Errorf("WAL 编码失败: %w", err)
	}
	if err := l.w.Flush(); err != nil {
		return fmt.Errorf("WAL flush 失败: %w", err)
	}
	if err := l.f.Sync(); err != nil {
		return fmt.Errorf("WAL fsync 失败: %w", err)
	}
	return nil
}

func validateRecord(rec Record) error {
	if len(rec.Labels) == 0 {
		return errors.New("WAL 记录缺少 labels")
	}
	if len(rec.Samples) == 0 {
		return errors.New("WAL 记录缺少 samples")
	}
	return nil
}

// Close 关闭文件。
func (l *WAL) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.w.Flush(); err != nil {
		l.f.Close()
		return err
	}
	return l.f.Close()
}

// Replay 逐行读取 WAL，对每条合法记录调用 fn。损坏的尾行（例如进程在
// 单次 write 中途崩溃）会被跳过并返回警告计数，不影响前面已确认的数据。
func Replay(path string, fn func(Record) error) (int, int, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, 0, nil
		}
		return 0, 0, fmt.Errorf("读取 WAL 失败: %w", err)
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	// 单条记录可能较大，放大 scanner 缓冲上限。
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	ok, bad := 0, 0
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec Record
		if err := json.Unmarshal(line, &rec); err != nil {
			bad++
			continue
		}
		if validateRecord(rec) != nil {
			bad++
			continue
		}
		if err := fn(rec); err != nil {
			return ok, bad, fmt.Errorf("重放第 %d 条记录失败: %w", ok+bad, err)
		}
		ok++
	}
	if err := sc.Err(); err != nil && !errors.Is(err, io.ErrUnexpectedEOF) {
		return ok, bad, fmt.Errorf("扫描 WAL 失败: %w", err)
	}
	return ok, bad, nil
}
