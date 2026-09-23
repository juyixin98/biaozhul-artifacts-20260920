// Package store 提供本地持久化样例：内存 map + JSONL 追加日志。
//
// 设计目标只是“本地持久化样例”，不是生产级数据库：
//   - 摄入按 (trace_id, span_id) upsert，全量重放 JSONL 即可恢复；
//   - 写操作用互斥锁串行化，每行一条 JSON（span 记录）；
//   - 同一 span 重复写入会在日志中留下多条记录，重放时“后者覆盖前者”。
package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"criticalpath/internal/analyzer"
)

// Store 保存所有 trace 的 span。
type Store struct {
	mu      sync.RWMutex
	traces  map[string]map[string]analyzer.Span
	logPath string
	logFile *os.File
	writer  *bufio.Writer
	replayN int
}

// record 是 JSONL 日志行：upsert 事件。
type record struct {
	Type string        `json:"type"` // 目前只有 "span"
	Span analyzer.Span `json:"span"`
}

// Open 打开（必要时创建）数据目录并重放日志。
func Open(dir string) (*Store, error) {
	if dir == "" {
		dir = "."
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("创建数据目录 %q: %w", dir, err)
	}
	s := &Store{
		traces:  map[string]map[string]analyzer.Span{},
		logPath: filepath.Join(dir, "spans.jsonl"),
	}
	if err := s.replay(); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(s.logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("打开追加日志: %w", err)
	}
	s.logFile = f
	s.writer = bufio.NewWriter(f)
	return s, nil
}

func (s *Store) replay() error {
	f, err := os.Open(s.logPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec record
		if err := json.Unmarshal(line, &rec); err != nil {
			return fmt.Errorf("日志行损坏: %w", err)
		}
		if rec.Type != "span" {
			continue
		}
		sp := rec.Span
		if s.traces[sp.TraceID] == nil {
			s.traces[sp.TraceID] = map[string]analyzer.Span{}
		}
		s.traces[sp.TraceID][sp.SpanID] = sp
		s.replayN++
	}
	return sc.Err()
}

// Upsert 写入或覆盖一个 span，先落盘后更新内存（崩溃时最多重复一条日志，重放幂等）。
func (s *Store) Upsert(sp analyzer.Span) error {
	if sp.TraceID == "" || sp.SpanID == "" {
		return errors.New("trace_id 和 span_id 均不能为空")
	}
	data, err := json.Marshal(record{Type: "span", Span: sp})
	if err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := s.writer.Write(data); err != nil {
		return err
	}
	if err := s.writer.WriteByte('\n'); err != nil {
		return err
	}
	if err := s.writer.Flush(); err != nil {
		return err
	}
	if err := s.logFile.Sync(); err != nil {
		return err
	}
	if s.traces[sp.TraceID] == nil {
		s.traces[sp.TraceID] = map[string]analyzer.Span{}
	}
	s.traces[sp.TraceID][sp.SpanID] = sp
	return nil
}

// PutBatch 批量 upsert，逐行追加（任一行失败即返回，已写行不回滚）。
func (s *Store) PutBatch(spans []analyzer.Span) (int, error) {
	for _, sp := range spans {
		if err := s.Upsert(sp); err != nil {
			return 0, err
		}
	}
	return len(spans), nil
}

// GetTrace 返回一个 trace 的全部 span，按 (start, span_id) 排序，便于稳定输出。
func (s *Store) GetTrace(traceID string) ([]analyzer.Span, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m, ok := s.traces[traceID]
	if !ok {
		return nil, false
	}
	out := make([]analyzer.Span, 0, len(m))
	for _, sp := range m {
		out = append(out, sp)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].StartUs != out[j].StartUs {
			return out[i].StartUs < out[j].StartUs
		}
		return out[i].SpanID < out[j].SpanID
	})
	return out, true
}

// TraceIDs 返回所有 trace id。
func (s *Store) TraceIDs() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.traces))
	for id := range s.traces {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// Close 刷盘并关闭日志文件。
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.writer.Flush(); err != nil {
		return err
	}
	return s.logFile.Close()
}

// ReplayedCount 返回启动时从重放日志恢复的记录数。
func (s *Store) ReplayedCount() int { return s.replayN }
