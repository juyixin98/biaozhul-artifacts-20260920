package store_test

import (
	"testing"

	"mirrorsec/internal/model"
	"mirrorsec/internal/store"
)

func TestSaveIsImmutable(t *testing.T) {
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r := &model.Report{ID: "rep_aaaa", Decision: model.StatusAllow, PolicyVersion: "1.0.0-frozen"}
	if err := st.Save(r); err != nil {
		t.Fatal(err)
	}
	// 同 ID 新结论必须被拒绝（重评估不得覆盖旧报告）。
	r2 := &model.Report{ID: "rep_aaaa", Decision: model.StatusDeny, PolicyVersion: "1.0.0-frozen"}
	if err := st.Save(r2); err == nil {
		t.Fatal("重复 ID 必须拒绝写入")
	}
	got, err := st.Get("rep_aaaa")
	if err != nil {
		t.Fatal(err)
	}
	if got.Decision != model.StatusAllow {
		t.Fatalf("旧报告被覆盖：got %s", got.Decision)
	}
}

func TestReevaluationCreatesNewReport(t *testing.T) {
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	first := &model.Report{ID: "rep_one", Decision: model.StatusUnknown, PolicyVersion: "1.0.0-frozen"}
	second := &model.Report{ID: "rep_two", Decision: model.StatusAllow, PolicyVersion: "1.0.0-frozen"}
	if err := st.Save(first); err != nil {
		t.Fatal(err)
	}
	if err := st.Save(second); err != nil {
		t.Fatal(err)
	}
	items, err := st.List(0)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 2 {
		t.Fatalf("重评估应产生两份独立报告，got %d", len(items))
	}
	if _, err := st.Get("rep_nope"); err != store.ErrNotFound {
		t.Fatalf("不存在报告应返回 ErrNotFound，got %v", err)
	}
	// 路径穿越必须被挡。
	if _, err := st.Get("../etc/passwd"); err != store.ErrNotFound {
		t.Fatalf("路径穿越应返回 ErrNotFound，got %v", err)
	}
}
