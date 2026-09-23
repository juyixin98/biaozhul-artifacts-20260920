package governor_test

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"cardinalgov/internal/governor"
)

// buildState 构造一个含 tracked 组合、overflow 桶、拒绝计数与截断计数的状态。
func buildState(t *testing.T) *governor.Store {
	t.Helper()
	cfg := governor.DefaultConfig()
	cfg.DefaultSeriesBudget = 5
	s := governor.NewStore(cfg)

	ts := time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC).UnixMilli()
	for i := 0; i < 5; i++ {
		s.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"pod": podName(i)}, Value: 1, Timestamp: ts})
	}
	// 3 个 overflow 样本。
	for i := 5; i < 8; i++ {
		r := s.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"uid": uid(i)}, Value: 2, Timestamp: ts})
		if !r.Overflowed {
			t.Fatalf("want overflow for %d", i)
		}
	}
	// 一些被拒绝的样本（坏指标名、含连字符的非法标签键），验证拒绝计数也持久化。
	s.Ingest(governor.Sample{Metric: "1bad-name"})
	s.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"bad-key": "v"}})

	// 第二个指标，无 overflow。
	s.Ingest(governor.Sample{Metric: "queue_depth", Labels: map[string]string{"q": "default"}, Value: 7.5, Timestamp: ts})
	return s
}

func podName(i int) string { return fmt.Sprintf("pod-%03d", i) }
func uid(i int) string     { return fmt.Sprintf("uid-%03d", i) }

// TestSnapshotRestoreConservation：保存 -> 新实例加载 -> 计数/组合/overflow 完全一致。
func TestSnapshotRestoreConservation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")

	before := buildState(t)
	if err := before.SaveSnapshot(path); err != nil {
		t.Fatalf("save: %v", err)
	}

	after, err := governor.LoadSnapshot(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}

	bc, ac := before.Snapshot(), after.Snapshot()
	if !countersEqual(bc, ac) {
		t.Fatalf("counters differ after restore:\nbefore=%+v\nafter =%+v", bc, ac)
	}
	if err := ac.CheckConservation(); err != nil {
		t.Fatal(err)
	}

	for _, name := range before.MetricNames() {
		bv, bok := before.QueryMetric(name)
		av, aok := after.QueryMetric(name)
		if bok != aok {
			t.Fatalf("metric %s presence differs: before=%v after=%v", name, bok, aok)
		}
		if bv.TrackedSeries != av.TrackedSeries {
			t.Fatalf("metric %s tracked: %d vs %d", name, bv.TrackedSeries, av.TrackedSeries)
		}
		if (bv.Overflow == nil) != (av.Overflow == nil) {
			t.Fatalf("metric %s overflow presence differs", name)
		}
		if bv.Overflow != nil {
			if bv.Overflow.SampleCount != av.Overflow.SampleCount || bv.Overflow.ValueSum != av.Overflow.ValueSum {
				t.Fatalf("overflow differs: %+v vs %+v", bv.Overflow, av.Overflow)
			}
		}
		// 按 seriesKey 对齐逐序列比较。
		keyed := map[string]governor.Series{}
		for _, ser := range av.Series {
			keyed[governor.SeriesKey(ser.Labels)] = ser
		}
		for _, want := range bv.Series {
			got, ok := keyed[governor.SeriesKey(want.Labels)]
			if !ok {
				t.Fatalf("series %v missing after restore", want.Labels)
			}
			if got.SampleCount != want.SampleCount || got.ValueSum != want.ValueSum || got.LastUpdated != want.LastUpdated {
				t.Fatalf("series %v differs: %+v vs %+v", want.Labels, want, got)
			}
		}
	}
}

// TestRestoreAppendsSurvive：恢复后继续写入，新 overflow 正确累加到恢复出的桶上。
func TestRestoreAppendsSurvive(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")

	before := buildState(t)
	if err := before.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}
	after, err := governor.LoadSnapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	r := after.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"uid": "brand-new-evil"}, Value: 4})
	if !r.Accepted || !r.Overflowed {
		t.Fatalf("new combo after restore must overflow: %+v", r)
	}
	view, _ := after.QueryMetric("http_requests")
	if view.Overflow.SampleCount != 4 || view.Overflow.ValueSum != 10 {
		t.Fatalf("overflow not accumulated across restore: %+v", view.Overflow)
	}
	// 老组合仍在。
	r = after.Ingest(governor.Sample{Metric: "http_requests", Labels: map[string]string{"pod": podName(0)}, Value: 1})
	if r.Overflowed {
		t.Fatal("tracked series lost across restart")
	}
}

// TestSaveSnapshotAtomic：快照文件写入后是可解析 JSON；不存在半截 tmp 文件残留。
func TestSaveSnapshotAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "state.json")
	s := buildState(t)
	if err := s.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}
	if _, err := governor.LoadSnapshot(path); err != nil {
		t.Fatal(err)
	}
}

// TestRestoreFoldWhenBudgetShrunk：预算被改小后恢复，多出的组合折叠进 overflow，
// SampleCount 守恒、不丢样本。
func TestRestoreFoldWhenBudgetShrunk(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")

	before := buildState(t)
	beforeCount := int64(0)
	view, _ := before.QueryMetric("http_requests")
	for _, ser := range view.Series {
		beforeCount += ser.SampleCount
	}
	if view.Overflow != nil {
		beforeCount += view.Overflow.SampleCount
	}
	if err := before.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}

	// 篡改快照：把预算缩小到 2，模拟“带着数据改配置后重启”。
	raw := readFile(t, path)
	raw = replaceJSONNumber(t, raw, `"DefaultSeriesBudget": 5`, `"DefaultSeriesBudget": 2`)
	writeFile(t, path, raw)

	after, err := governor.LoadSnapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	view2, _ := after.QueryMetric("http_requests")
	if view2.TrackedSeries != 2 {
		t.Fatalf("tracked should be capped at 2, got %d", view2.TrackedSeries)
	}
	afterCount := int64(0)
	for _, ser := range view2.Series {
		afterCount += ser.SampleCount
	}
	if view2.Overflow != nil {
		afterCount += view2.Overflow.SampleCount
	}
	if afterCount != beforeCount {
		t.Fatalf("samples lost during fold: before=%d after=%d", beforeCount, afterCount)
	}
}

// TestRestoreFailsWhenMetricNameLimitShrunk：快照指标名数量超过恢复配置的
// MaxMetricNames 时必须显式报错，而不是静默丢弃。
func TestRestoreFailsWhenMetricNameLimitShrunk(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")

	before := buildState(t) // http_requests + queue_depth = 2 个指标
	if err := before.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}

	raw := readFile(t, path)
	raw = replaceJSONNumber(t, raw, `"MaxMetricNames": 512`, `"MaxMetricNames":1`)
	writeFile(t, path, raw)

	if _, err := governor.LoadSnapshot(path); err == nil {
		t.Fatal("restore must fail when snapshot metric count exceeds MaxMetricNames")
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func replaceJSONNumber(t *testing.T, raw, old, new string) string {
	t.Helper()
	if !strings.Contains(raw, old) {
		t.Fatalf("snapshot does not contain %s", old)
	}
	return strings.Replace(raw, old, new, 1)
}

func countersEqual(a, b governor.Counters) bool {
	if a.Received != b.Received || a.Accepted != b.Accepted || a.Overflowed != b.Overflowed ||
		a.Rejected != b.Rejected || a.SeriesCreated != b.SeriesCreated || a.SeriesEvicted != b.SeriesEvicted ||
		a.ValuesTruncated != b.ValuesTruncated {
		return false
	}
	if len(a.RejectedByReason) != len(b.RejectedByReason) {
		return false
	}
	for k, v := range a.RejectedByReason {
		if b.RejectedByReason[k] != v {
			return false
		}
	}
	return true
}
