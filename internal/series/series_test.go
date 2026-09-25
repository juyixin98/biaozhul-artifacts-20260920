package series

import (
	"errors"
	"math"
	"testing"
)

func TestValidateRejectsNegativeAndNonFinite(t *testing.T) {
	cases := []struct {
		name string
		s    Sample
		want error
	}{
		{"负值拒绝", Sample{TimestampMs: 1, Value: -0.0001}, ErrNegativeValue},
		{"-1 拒绝", Sample{TimestampMs: 1, Value: -1}, ErrNegativeValue},
		{"NaN 拒绝", Sample{TimestampMs: 1, Value: math.NaN()}, ErrNonFinite},
		{"+Inf 拒绝", Sample{TimestampMs: 1, Value: math.Inf(1)}, ErrNonFinite},
		{"-Inf 拒绝", Sample{TimestampMs: 1, Value: math.Inf(-1)}, ErrNonFinite},
		{"零值合法", Sample{TimestampMs: 1, Value: 0}, nil},
		{"正值合法", Sample{TimestampMs: 1, Value: 42.5}, nil},
		{"坏时间戳", Sample{TimestampMs: 0, Value: 1}, ErrBadTimestamp},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.s.Validate()
			if !errors.Is(err, tc.want) {
				t.Fatalf("Validate(%v) = %v, 期望 %v", tc.s.Value, err, tc.want)
			}
		})
	}
}

func TestValidateBatchRejectsAtomic(t *testing.T) {
	samples := []Sample{
		{TimestampMs: 10, Value: 1},
		{TimestampMs: 20, Value: -5},         // 非法
		{TimestampMs: 30, Value: math.NaN()}, // 非法
	}
	rej := ValidateBatch(samples)
	if len(rej) != 2 {
		t.Fatalf("拒绝数量 = %d, 期望 2", len(rej))
	}
	if rej[0].Index != 1 || rej[1].Index != 2 {
		t.Fatalf("拒绝下标 = %v", rej)
	}
}

func TestNormalizeOutOfOrderDuplicates(t *testing.T) {
	// 乱序到达；同时间戳同值重复 1 个；同时间戳冲突 1 组（max-wins 保留 40）。
	in := []Sample{
		{TimestampMs: 30, Value: 30},
		{TimestampMs: 10, Value: 10},
		{TimestampMs: 20, Value: 20},
		{TimestampMs: 10, Value: 10}, // 同值重复
		{TimestampMs: 20, Value: 15}, // 冲突：应保留 20
		{TimestampMs: 20, Value: 20}, // 同值重复
	}
	res := Normalize(in)
	want := []Sample{{10, 10}, {20, 20}, {30, 30}}
	if len(res.Samples) != len(want) {
		t.Fatalf("归一化后数量 = %d (%v), 期望 %d", len(res.Samples), res.Samples, len(want))
	}
	for i := range want {
		if res.Samples[i] != want[i] {
			t.Fatalf("第 %d 项 = %+v, 期望 %+v；全部 = %v", i, res.Samples[i], want[i], res.Samples)
		}
	}
	if res.DuplicateSame != 2 {
		t.Fatalf("DuplicateSame = %d, 期望 2", res.DuplicateSame)
	}
	if res.DuplicateConflict != 1 {
		t.Fatalf("DuplicateConflict = %d, 期望 1", res.DuplicateConflict)
	}
}

func TestNormalizeDoesNotMutateInput(t *testing.T) {
	in := []Sample{{TimestampMs: 20, Value: 1}, {TimestampMs: 10, Value: 1}}
	_ = Normalize(in)
	if in[0].TimestampMs != 20 {
		t.Fatal("输入切片被原地修改")
	}
}
