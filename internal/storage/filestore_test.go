package storage

import (
	"path/filepath"
	"testing"
	"time"

	"tracestitch/internal/assembler"
	"tracestitch/internal/clock"
	"tracestitch/internal/model"
)

// WAL + 快照：写入 -> 重放必须重建全部修订（含 timeout/late），
// 快照文件按 trace 落盘。
func TestWALReplayRebuildsRevisions(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "store")
	store, err := Open(dir)
	if err != nil {
		t.Fatalf("open: %v", err)
	}

	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	fc := clock.NewFake(base)
	cfg := assembler.Config{Timeout: 5 * time.Second, ClockSkewTolerance: time.Millisecond}
	asm := assembler.New(cfg, fc, store, nil)

	child := model.Span{
		TraceID: "TR", SpanID: "child", ParentSpanID: "root",
		ServiceName: "svc-child", Name: "op",
		StartUnixNano: 100, EndUnixNano: 200,
	}
	if r := asm.Ingest(child); r.Status != "accepted" {
		t.Fatalf("ingest child: %s", r.Error)
	}
	fc.Advance(6 * time.Second)
	if n := asm.Sweep(); n != 1 {
		t.Fatalf("sweep=%d want 1", n)
	}
	// 迟到根补全。
	root := model.Span{
		TraceID: "TR", SpanID: "root",
		ServiceName: "svc-root", Name: "root",
		StartUnixNano: 0, EndUnixNano: 300,
	}
	if r := asm.Ingest(root); r.Reason != assembler.ReasonLate {
		t.Fatalf("root reason=%s want late", r.Reason)
	}

	before, ok := asm.GetTrace("TR")
	if !ok {
		t.Fatal("trace missing")
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// 全新实例：只从 WAL 重建，不触碰 store。
	store2, err := Open(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	recs, err := store2.ReadWAL()
	if err != nil {
		t.Fatalf("read wal: %v", err)
	}
	if len(recs) != 3 { // child span, timeout event, root span
		t.Fatalf("wal records=%d want 3", len(recs))
	}
	if recs[1].Event != assembler.EventTimeout {
		t.Fatalf("record[1] event=%q want timeout", recs[1].Event)
	}

	fc2 := clock.NewFake(base.Add(time.Hour)) // 重放不依赖“现在”的钟
	asm2 := assembler.New(cfg, fc2, store2, nil)
	asm2.Replay(recs)

	after, ok := asm2.GetTrace("TR")
	if !ok {
		t.Fatal("replayed trace missing")
	}
	if len(after.Revisions) != len(before.Revisions) {
		t.Fatalf("replayed revisions=%d want %d", len(after.Revisions), len(before.Revisions))
	}
	for i := range before.Revisions {
		b, a := before.Revisions[i], after.Revisions[i]
		if b.Reason != a.Reason || b.Version != a.Version ||
			b.Complete != a.Complete || b.MissingRoot != a.MissingRoot {
			t.Fatalf("rev %d diverges after replay:\nbefore=%+v\nafter =%+v", i+1, b, a)
		}
		if len(b.SpanIDs) != len(a.SpanIDs) {
			t.Fatalf("rev %d span count diverges", i+1)
		}
		for j := range b.SpanIDs {
			if b.SpanIDs[j] != a.SpanIDs[j] {
				t.Fatalf("rev %d spanIds diverge: %v vs %v", i+1, b.SpanIDs, a.SpanIDs)
			}
		}
	}
	if !after.Complete || after.Sealed {
		t.Fatalf("replayed state: complete=%v sealed=%v want true/false", after.Complete, after.Sealed)
	}
	if v := asm2.CheckRevisionContainment("TR"); v != nil {
		t.Fatalf("containment after replay: %v", v)
	}

	// 快照文件存在且内容为最终视图。
	files, err := store2.SnapshotFiles()
	if err != nil {
		t.Fatalf("snapshot files: %v", err)
	}
	if len(files) != 1 || files[0] != "TR.json" {
		t.Fatalf("snapshot files=%v want [TR.json]", files)
	}
}

// 空目录可直接打开（首次运行）。
func TestOpenEmptyDir(t *testing.T) {
	store, err := Open(t.TempDir())
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	recs, err := store.ReadWAL()
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if len(recs) != 0 {
		t.Fatalf("empty wal returned %d records", len(recs))
	}
}
