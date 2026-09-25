package store

import (
	"testing"

	"counterreset/internal/series"
)

func labelsOf() map[string]string {
	return map[string]string{"__name__": "m", "instance": "i1"}
}

func TestIngestRejectsBadBatchAtomically(t *testing.T) {
	st := New()
	good := []series.Sample{{TimestampMs: 10, Value: 1}}
	if _, _, err := st.Ingest(labelsOf(), good); err != nil {
		t.Fatal(err)
	}
	// 第二批含负值：整批拒绝，序列保持原状。
	bad := []series.Sample{
		{TimestampMs: 20, Value: 2},
		{TimestampMs: 30, Value: -9},
	}
	_, rejected, err := st.Ingest(labelsOf(), bad)
	if err != nil {
		t.Fatal(err)
	}
	if len(rejected) != 1 {
		t.Fatalf("拒绝数 = %d", len(rejected))
	}
	m := st.Get(labelsOf())
	if m == nil || len(m.Samples) != 1 {
		t.Fatalf("整批拒绝后状态被污染: %+v", m)
	}
}

func TestIngestMergeDedupAcrossBatches(t *testing.T) {
	st := New()
	l := labelsOf()
	// 乱序第一批。
	b1 := []series.Sample{{TimestampMs: 30, Value: 3}, {TimestampMs: 10, Value: 1}}
	if _, _, err := st.Ingest(l, b1); err != nil {
		t.Fatal(err)
	}
	// 第二批：重复同值、冲突（ts=10 更大值 5 → max-wins）、新点。
	b2 := []series.Sample{
		{TimestampMs: 10, Value: 1},
		{TimestampMs: 10, Value: 5},
		{TimestampMs: 20, Value: 2},
	}
	res, _, err := st.Ingest(l, b2)
	if err != nil {
		t.Fatal(err)
	}
	if res.UniqueSamples != 3 {
		t.Fatalf("唯一样本数 = %d, 期望 3", res.UniqueSamples)
	}
	m := st.Get(l)
	want := []series.Sample{{TimestampMs: 10, Value: 5}, {TimestampMs: 20, Value: 2}, {TimestampMs: 30, Value: 3}}
	for i := range want {
		if m.Samples[i] != want[i] {
			t.Fatalf("合并结果异常: %v", m.Samples)
		}
	}
	// b2 内部 ts=10 有 {5,1}：归一化保留最大值 5，1 与保留值冲突
	// → batch same=0 conflict=1。
	if res.BatchDuplicateSame != 0 || res.BatchDuplicateConflict != 1 {
		t.Fatalf("批次内统计异常: same=%d conflict=%d",
			res.BatchDuplicateSame, res.BatchDuplicateConflict)
	}
	// 与历史重叠：ts=10 上 b1 为 1，b2 归一化后为 5 → 1 次冲突覆盖。
	if res.OverlapSame != 0 || res.OverlapConflict != 1 {
		t.Fatalf("重叠统计异常: same=%d conflict=%d", res.OverlapSame, res.OverlapConflict)
	}
	if res.TotalDuplicateConflict != 2 {
		t.Fatalf("累计冲突应为 2（批次内 1 + 覆盖历史 1）, 实际 %d", res.TotalDuplicateConflict)
	}

	// 第三批：原样重放历史数据，应全部幂等（3 次同值重叠）。
	res3, _, err := st.Ingest(l, want)
	if err != nil {
		t.Fatal(err)
	}
	if res3.OverlapSame != 3 || res3.OverlapConflict != 0 || res3.UniqueSamples != 3 {
		t.Fatalf("幂等重放异常: %+v", res3)
	}
}

func TestGetMissingAndKeyStability(t *testing.T) {
	st := New()
	if got := st.Get(map[string]string{"x": "y"}); got != nil {
		t.Fatal("不存在的序列应返回 nil")
	}
	k1 := Key(map[string]string{"a": "1", "b": "2"})
	k2 := Key(map[string]string{"b": "2", "a": "1"})
	if k1 != k2 {
		t.Fatal("标签键应与声明顺序无关")
	}
}

func TestIngestValidationErrors(t *testing.T) {
	st := New()
	if _, _, err := st.Ingest(nil, []series.Sample{{TimestampMs: 1, Value: 1}}); err == nil {
		t.Fatal("空 labels 应报错")
	}
	if _, _, err := st.Ingest(labelsOf(), nil); err == nil {
		t.Fatal("空 samples 应报错")
	}
}
