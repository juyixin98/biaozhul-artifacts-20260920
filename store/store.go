// Package store 提供 traceassembly 的本地持久化样例实现：
//   - wal.jsonl：只追加的摄入事件日志（span / watermark），是恢复的唯一事实来源；
//   - snapshots/<traceID>/rev-<n>.json：每次修订的不可变快照，原子重命名写入，
//     仅供离线查看，恢复时以重放 WAL 重新生成。
package store

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"traceassembly/trace"
)

// FileStore 是 Sink 的本地文件实现，并发安全。
type FileStore struct {
	dir string
	mu  chan struct{} // 容量为 1 的信号量，充当互斥锁
	f   *os.File
	w   *bufio.Writer
	enc *json.Encoder
}

// walRecord 是 WAL 中的一条记录。Type 取 "span"、"watermark" 或 "sweep"。
type walRecord struct {
	Type      string      `json:"type"`
	Span      *trace.Span `json:"span,omitempty"`
	Watermark int64       `json:"watermark_ns,omitempty"`
}

// Open 打开（不存在则创建）数据目录，重放已有 WAL 构造组装器，
// 并把返回的组装器持久化出口接到本 store。
func Open(dir string, cfg trace.Config) (*trace.Assembler, *FileStore, error) {
	if err := os.MkdirAll(filepath.Join(dir, "snapshots"), 0o755); err != nil {
		return nil, nil, err
	}
	s := &FileStore{dir: dir, mu: make(chan struct{}, 1)}
	s.mu <- struct{}{}

	path := filepath.Join(dir, "wal.jsonl")
	records, err := readWAL(path)
	if err != nil {
		return nil, nil, err
	}

	// 重放期间 sink 为 nil：不重复追加 WAL；修订产生时再挂上 store 重写快照。
	asm := trace.New(cfg, nil)
	var replayed []trace.Revision
	for _, r := range records {
		switch r.Type {
		case "span":
			asm.RestoreApplySpan(*r.Span)
		case "watermark":
			replayed = append(replayed, asm.RestoreAdvanceWatermark(r.Watermark)...)
		case "sweep":
			replayed = append(replayed, asm.RestoreSweep()...)
		default:
			return nil, nil, fmt.Errorf("store: unknown wal record type %q", r.Type)
		}
	}

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, err
	}
	s.f = f
	s.w = bufio.NewWriter(f)
	s.enc = json.NewEncoder(s.w)

	// 挂上 sink 后重放期的修订尚未有快照文件：按编号重写一遍（确定性覆盖）。
	// 通过反射式小技巧不可行，改为：新进程首次写入快照时若文件已存在则覆盖。
	asm.AttachSink(s)
	for _, rev := range replayed {
		if err := s.SaveRevision(rev); err != nil {
			return nil, nil, fmt.Errorf("store: rewrite snapshot during replay: %w", err)
		}
	}
	return asm, s, nil
}

// Close 落盘缓冲并关闭 WAL。
func (s *FileStore) Close() error {
	<-s.mu
	defer func() { s.mu <- struct{}{} }()
	if err := s.w.Flush(); err != nil {
		return err
	}
	if err := s.f.Sync(); err != nil {
		return err
	}
	return s.f.Close()
}

func readWAL(path string) ([]walRecord, error) {
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var records []walRecord
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	line := 0
	for sc.Scan() {
		line++
		text := sc.Bytes()
		if len(text) == 0 {
			continue
		}
		var r walRecord
		if err := json.Unmarshal(text, &r); err != nil {
			return nil, fmt.Errorf("store: wal line %d: %w", line, err)
		}
		records = append(records, r)
	}
	return records, sc.Err()
}

// AppendSpan 实现 trace.Sink。
func (s *FileStore) AppendSpan(sp trace.Span) error {
	return s.write(walRecord{Type: "span", Span: &sp})
}

// AppendWatermark 实现 trace.Sink。
func (s *FileStore) AppendWatermark(ns int64) error {
	return s.write(walRecord{Type: "watermark", Watermark: ns})
}

// AppendSweep 实现 trace.Sink。
func (s *FileStore) AppendSweep() error {
	return s.write(walRecord{Type: "sweep"})
}

func (s *FileStore) write(r walRecord) error {
	<-s.mu
	defer func() { s.mu <- struct{}{} }()
	if err := s.enc.Encode(r); err != nil {
		return err
	}
	if err := s.w.Flush(); err != nil {
		return err
	}
	return s.f.Sync() // 样例级持久化：每条事件 fsync
}

// SaveRevision 实现 trace.Sink：原子重命名写快照。
func (s *FileStore) SaveRevision(rev trace.Revision) error {
	dir := filepath.Join(s.dir, "snapshots", rev.TraceID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	final := filepath.Join(dir, fmt.Sprintf("rev-%d.json", rev.Revision))
	tmp := final + ".tmp"
	data, err := json.MarshalIndent(rev, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, final)
}
