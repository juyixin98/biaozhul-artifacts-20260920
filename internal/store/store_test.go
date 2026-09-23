package store

import (
	"path/filepath"
	"testing"

	"criticalpath/internal/analyzer"
)

func TestUpsertAndReplay(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")

	st, err := Open(dir)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	spans := analyzer.DemoSpans("demo")
	if n, err := st.PutBatch(spans); err != nil || n != len(spans) {
		t.Fatalf("PutBatch = %d, %v", n, err)
	}
	// 覆盖写入同一个 span（upsert 语义，重放后以后者为准）。
	updated := spans[0]
	updated.Name = "request-renamed"
	if err := st.Upsert(updated); err != nil {
		t.Fatalf("Upsert 更新: %v", err)
	}
	if err := st.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	// 重新打开，从 JSONL 重放恢复。
	st2, err := Open(dir)
	if err != nil {
		t.Fatalf("重开 Open: %v", err)
	}
	defer st2.Close()
	if st2.ReplayedCount() != len(spans)+1 {
		t.Errorf("重放记录数 = %d，期望 %d", st2.ReplayedCount(), len(spans)+1)
	}
	got, ok := st2.GetTrace("demo")
	if !ok {
		t.Fatal("重放后 trace 不存在")
	}
	if len(got) != len(spans) {
		t.Fatalf("span 数 = %d，期望 %d（upsert 不应新增）", len(got), len(spans))
	}
	if got[0].Name != "request-renamed" {
		t.Errorf("upsert 未生效，按 start 排序首个 span = %+v", got[0])
	}
	if ids := st2.TraceIDs(); len(ids) != 1 || ids[0] != "demo" {
		t.Errorf("TraceIDs = %v", ids)
	}
}

func TestUpsertValidation(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if err := st.Upsert(analyzer.Span{SpanID: "x"}); err == nil {
		t.Error("空 trace_id 必须报错")
	}
	if err := st.Upsert(analyzer.Span{TraceID: "t"}); err == nil {
		t.Error("空 span_id 必须报错")
	}
}
