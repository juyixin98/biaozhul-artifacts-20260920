package store

import (
	"reflect"
	"testing"
	"time"

	"traceassembly/trace"
)

var sT0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func sp(tr, id, parent, svc string, recvNS, startMS, durMS int64) trace.Span {
	return trace.Span{
		TraceID:       tr,
		SpanID:        id,
		ParentSpanID:  parent,
		Service:       svc,
		Operation:     "op-" + id,
		Start:         sT0.Add(time.Duration(startMS) * time.Millisecond),
		DurationNanos: (time.Duration(durMS) * time.Millisecond).Nanoseconds(),
		ReceiveNS:     recvNS,
	}
}

func cfg() trace.Config { return trace.Config{TimeoutNS: 100, SkewToleranceNS: 1000} }

// 完整生命周期：缺根关闭 → 迟到根修订，关闭后重开进程，状态与快照逐字节级一致。
func TestWALReplayReproducesRevisions(t *testing.T) {
	dir := t.TempDir()

	// --- 第一次进程：制造 rev1（缺根超时）与 rev2（迟到补全）---
	asm, st, err := Open(dir, cfg())
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := asm.IngestBatch([]trace.Span{
		sp("tr1", "g", "c", "edge", 20, 30, 5),
		sp("tr1", "c", "root", "api", 10, 10, 40),
	}, -1); err != nil {
		t.Fatal(err)
	}
	if _, err := asm.AdvanceWatermark(200); err != nil {
		t.Fatal(err)
	}
	if _, _, err := asm.IngestBatch([]trace.Span{
		sp("tr1", "root", "", "gw", 300, 0, 100),
	}, -1); err != nil {
		t.Fatal(err)
	}
	if _, err := asm.AdvanceWatermark(500); err != nil {
		t.Fatal(err)
	}
	// 另有一个完整 trace 与一个重复/冲突，丰富回放内容。
	if _, _, err := asm.IngestBatch([]trace.Span{
		sp("tr2", "root2", "", "gw", 10, 0, 50),
		sp("tr2", "x", "root2", "api", 20, 5, 10),
		sp("tr2", "x", "root2", "api", 25, 5, 10), // 精确重复
		sp("tr2", "x", "root2", "api", 30, 5, 11), // 冲突
	}, -1); err != nil {
		t.Fatal(err)
	}
	if _, err := asm.AdvanceWatermark(600); err != nil {
		t.Fatal(err)
	}

	before := snapshot(t, asm)
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// --- 第二次进程：仅靠 WAL 重建 ---
	asm2, st2, err := Open(dir, cfg())
	if err != nil {
		t.Fatal(err)
	}
	after := snapshot(t, asm2)
	if !reflect.DeepEqual(before, after) {
		t.Fatalf("replayed state differs:\n before=%#v\n after =%#v", before, after)
	}
	if w := asm2.Watermark(); w != 600 {
		t.Fatalf("watermark restored wrong: %d", w)
	}
	// 重放后功能继续可用：再摄入并产生新事件也能落盘。
	if _, _, err := asm2.IngestBatch([]trace.Span{
		sp("tr3", "z", "", "gw", 700, 0, 5),
	}, -1); err != nil {
		t.Fatal(err)
	}
	if err := st2.Close(); err != nil {
		t.Fatal(err)
	}

	// --- 第三次进程：确认第二次进程的增量也持久化 ---
	asm3, st3, err := Open(dir, cfg())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok, _ := asm3.GetTrace("tr3"); !ok {
		t.Fatal("tr3 missing after second replay")
	}
	_ = st3.Close()
}

type fullSnapshot struct {
	traces  []trace.TraceSummary
	rev1    trace.Revision
	rev2    trace.Revision
	tr2Rev1 trace.Revision
}

func snapshot(t *testing.T, asm *trace.Assembler) fullSnapshot {
	t.Helper()
	s := fullSnapshot{traces: asm.ListTraces()}
	var ok bool
	if s.rev1, ok = asm.GetRevision("tr1", 1); !ok {
		t.Fatal("tr1 rev1 missing")
	}
	if s.rev2, ok = asm.GetRevision("tr1", 2); !ok {
		t.Fatal("tr1 rev2 missing")
	}
	if s.tr2Rev1, ok = asm.GetRevision("tr2", 1); !ok {
		t.Fatal("tr2 rev1 missing")
	}
	return s
}
