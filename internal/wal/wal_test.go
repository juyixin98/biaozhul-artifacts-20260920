package wal

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAppendReplayAndReopen(t *testing.T) {
	dir := t.TempDir()
	payload := func(v string) map[string]any { return map[string]any{"txnId": v} }

	// 第一个“进程”：写入三条记录。
	w1, err := Open(dir, "node-x")
	if err != nil {
		t.Fatal(err)
	}
	if err := w1.Append("PART_PREPARED", payload("t1")); err != nil {
		t.Fatal(err)
	}
	if err := w1.Append("PART_COMMITTED", payload("t1")); err != nil {
		t.Fatal(err)
	}
	if err := w1.Append("PART_ABORTED", payload("t2")); err != nil {
		t.Fatal(err)
	}
	if w1.Total() != 3 {
		t.Fatalf("Total=%d, 期望 3", w1.Total())
	}
	if err := w1.Close(); err != nil {
		t.Fatal(err)
	}

	// “崩溃重启”：重新打开同一文件，历史记录必须全部保留。
	w2, err := Open(dir, "node-x")
	if err != nil {
		t.Fatal(err)
	}
	defer w2.Close()
	var got []string
	if err := w2.Replay(func(r Record) error {
		got = append(got, r.Type)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	want := []string{"PART_PREPARED", "PART_COMMITTED", "PART_ABORTED"}
	if len(got) != len(want) {
		t.Fatalf("重放记录数=%d, 期望 %d (%v)", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("第 %d 条类型=%s, 期望 %s", i, got[i], want[i])
		}
	}

	// resume 追加：历史记录不重复、不丢失。
	if err := w2.Append("PART_PREPARED", payload("t3")); err != nil {
		t.Fatal(err)
	}
	if w2.Total() != 4 {
		t.Fatalf("resume 后 Total=%d, 期望 4", w2.Total())
	}
}

func TestCorruptLineReported(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "bad.wal")
	if err := os.WriteFile(p, []byte("{not json\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	w, err := Open(dir, "bad")
	if err == nil {
		w.Close()
		t.Fatal("损坏的 WAL 应当返回错误")
	}
}
