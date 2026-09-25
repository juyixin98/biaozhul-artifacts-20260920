package store

import (
	"path/filepath"
	"testing"

	"cpathtrace/internal/model"
)

func TestSaveGetListDelete(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	st, err := NewFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}

	tr := model.Trace{TraceID: "t1", Spans: []model.Span{
		{SpanID: "a", Name: "a", StartTime: 0, EndTime: 1},
	}}
	if err := st.Save(tr); err != nil {
		t.Fatal(err)
	}
	got, err := st.Get("t1")
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Spans) != 1 || got.Spans[0].SpanID != "a" {
		t.Fatalf("roundtrip mismatch: %+v", got)
	}

	// Overwrite.
	tr.Spans = append(tr.Spans, model.Span{SpanID: "b", Name: "b"})
	if err := st.Save(tr); err != nil {
		t.Fatal(err)
	}
	got, _ = st.Get("t1")
	if len(got.Spans) != 2 {
		t.Fatalf("overwrite failed: %d spans", len(got.Spans))
	}

	// List ordering.
	if err := st.Save(model.Trace{TraceID: "t2"}); err != nil {
		t.Fatal(err)
	}
	ids, err := st.List()
	if err != nil || len(ids) != 2 || ids[0] != "t1" || ids[1] != "t2" {
		t.Fatalf("list = %v, err=%v", ids, err)
	}

	// Delete + not-found semantics.
	if err := st.Delete("t1"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Get("t1"); err != ErrNotFound {
		t.Fatalf("get deleted = %v, want ErrNotFound", err)
	}
	if err := st.Delete("t1"); err != ErrNotFound {
		t.Fatalf("delete missing = %v, want ErrNotFound", err)
	}
}

func TestRejectsPathTraversal(t *testing.T) {
	st, err := NewFileStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"../escape", "a/b", `a\b`, ""} {
		if _, err := st.Get(bad); err == nil {
			t.Fatalf("expected rejection of trace id %q", bad)
		}
	}
}
