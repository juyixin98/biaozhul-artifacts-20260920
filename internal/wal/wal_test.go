package wal

import (
	"os"
	"path/filepath"
	"testing"

	"counterreset/internal/series"
)

func openAppend(t *testing.T, path string) *os.File {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func recFor(vals ...float64) Record {
	s := make([]series.Sample, len(vals))
	for i, v := range vals {
		s[i] = series.Sample{TimestampMs: int64(i + 1), Value: v}
	}
	return Record{Labels: map[string]string{"k": "v"}, Samples: s}
}

func TestAppendReplayRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "wal.jsonl")

	l, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(recFor(1, 2, 3)); err != nil {
		t.Fatal(err)
	}
	// 追加乱序样本，验证重放交给上层归一化。
	if err := l.Append(Record{
		Labels:  map[string]string{"k": "v"},
		Samples: []series.Sample{{TimestampMs: 2, Value: 9}, {TimestampMs: 4, Value: 4}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}

	var got []Record
	ok, bad, err := Replay(path, func(r Record) error {
		got = append(got, r)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if ok != 2 || bad != 0 {
		t.Fatalf("重放计数 ok=%d bad=%d", ok, bad)
	}
	if len(got) != 2 || len(got[0].Samples) != 3 {
		t.Fatalf("重放数据异常: %+v", got)
	}
	if got[1].Samples[0].Value != 9 {
		t.Fatalf("乱序数据未原样重放: %+v", got[1])
	}
}

func TestReplayMissingFile(t *testing.T) {
	ok, bad, err := Replay(filepath.Join(t.TempDir(), "nope.jsonl"), func(Record) error { return nil })
	if err != nil || ok != 0 || bad != 0 {
		t.Fatalf("不存在的 WAL 应视为空: %v %d %d", err, ok, bad)
	}
}

func TestReplaySkipsCorruptTail(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "wal.jsonl")

	l, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(recFor(1)); err != nil {
		t.Fatal(err)
	}
	l.Close()

	// 手工追加损坏行，模拟写一半崩溃的尾记录。
	f := openAppend(t, path)
	if _, err := f.WriteString("{\"labels\":{\"k\":\"v\"},\"samples\":[{\"ts_ms\":2,\"value\":2}]}\n"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("{not json\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()

	ok, bad, err := Replay(path, func(Record) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	if ok != 2 || bad != 1 {
		t.Fatalf("损坏尾行应被跳过: ok=%d bad=%d", ok, bad)
	}
}

func TestAppendRejectsInvalid(t *testing.T) {
	l, err := Open(filepath.Join(t.TempDir(), "w.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	if err := l.Append(Record{Labels: nil, Samples: []series.Sample{{TimestampMs: 1, Value: 1}}}); err == nil {
		t.Fatal("缺 labels 应拒绝")
	}
	if err := l.Append(Record{Labels: map[string]string{"k": "v"}, Samples: nil}); err == nil {
		t.Fatal("缺 samples 应拒绝")
	}
}
