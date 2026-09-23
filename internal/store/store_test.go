package store_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"mirror-admission/internal/model"
	"mirror-admission/internal/store"
)

func TestJSONLAppendOnlyAndReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "reports.jsonl")

	s1, err := store.NewJSONL(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	r1 := &model.Report{ID: "rpt_a", Decision: "ALLOW", PolicyVersion: "1.4.0"}
	if err := s1.Save(ctx, r1); err != nil {
		t.Fatal(err)
	}
	r2 := &model.Report{ID: "rpt_b", Decision: "DENY", PolicyVersion: "1.4.0"}
	if err := s1.Save(ctx, r2); err != nil {
		t.Fatal(err)
	}

	// Reopen from disk: history must survive and stay in append order.
	s2, err := store.NewJSONL(path)
	if err != nil {
		t.Fatal(err)
	}
	got, err := s2.Get(ctx, "rpt_a")
	if err != nil || got.Decision != "ALLOW" {
		t.Fatalf("reopen get: %+v err=%v", got, err)
	}
	list, err := s2.List(ctx)
	if err != nil || len(list) != 2 || list[0].ID != "rpt_a" || list[1].ID != "rpt_b" {
		t.Fatalf("append order/history broken: %+v err=%v", list, err)
	}

	// Reusing an existing id must refuse, never overwrite.
	if err := s2.Save(ctx, &model.Report{ID: "rpt_a", Decision: "DENY"}); err == nil {
		t.Fatal("duplicate id overwrite must be refused")
	}
	got, _ = s2.Get(ctx, "rpt_a")
	if got.Decision != "ALLOW" {
		t.Fatal("original report was overwritten")
	}

	if _, err := s2.Get(ctx, "rpt_missing"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}
