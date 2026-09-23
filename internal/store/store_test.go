package store

import (
	"fmt"
	"math"
	"sort"
	"testing"

	"metricsink/internal/agg"
	"metricsink/internal/model"
	"metricsink/internal/synth"
)

// 固定基准时间：2025-01-01 01:00:00 UTC（恰为整点，便于构造跨边界用例）。
const base int64 = 1735693200

func labelsOf(host string) map[string]string {
	return map[string]string{"host": host}
}

func sample(id string, ts int64, v float64) model.Sample {
	return model.Sample{ID: id, Metric: "m", Labels: labelsOf("h"), Ts: ts, Value: v}
}

func keyOf(s model.Sample) model.SeriesKey { return s.Key() }

// assertLayersMatchRaw 是核心对账：分钟层与小时层都必须与“直接从原始样本
// 重算”逐桶一致（count 精确相等，sum/min/max 浮点容差内相等）。
func assertLayersMatchRaw(t *testing.T, st *Store, sk model.SeriesKey, start, end int64) {
	t.Helper()
	for _, w := range []int64{model.MinuteWindow, model.HourWindow} {
		got := st.RollupPoints(string(sk), w, start, end, true)
		want := st.RecomputeFromRaw(string(sk), w, start, end, true)
		if len(got) != len(want) {
			t.Fatalf("窗口 %d: 桶数 %d != 重算桶数 %d", w, len(got), len(want))
		}
		for i := range got {
			if got[i].Ts != want[i].Ts {
				t.Fatalf("窗口 %d: 第 %d 桶时间 %d != %d", w, i, got[i].Ts, want[i].Ts)
			}
			gb := agg.Bucket{Count: got[i].Count, Sum: got[i].Sum, Min: got[i].Min, Max: got[i].Max}
			wb := agg.Bucket{Count: want[i].Count, Sum: want[i].Sum, Min: want[i].Min, Max: want[i].Max}
			if !gb.AlmostEqual(wb, 1e-9) {
				t.Fatalf("窗口 %d 桶 ts=%d (%s): 增量层 %+v 与原始重算 %+v 不一致",
					w, got[i].Ts, model.FormatTs(got[i].Ts), gb, wb)
			}
			if got[i].Count == 0 && got[i].Avg != nil {
				t.Fatalf("空桶 ts=%d 的 avg 应为 null", got[i].Ts)
			}
			if got[i].Count > 0 && got[i].Avg == nil {
				t.Fatalf("非空桶 ts=%d 的 avg 不应为 null", got[i].Ts)
			}
		}
	}
}

// TestDensitiesVsRawRecompute 在四种样本密度下（跨 3 个小时边界），
// 验证增量降采样结果与原始重算一致。这是验收主测试。
func TestDensitiesVsRawRecompute(t *testing.T) {
	start := base
	end := base + 3*3600 - 1 // 覆盖 3 个小时桶、180 个分钟桶
	for _, d := range []synth.Density{synth.Dense, synth.Sparse, synth.Ragged, synth.Gappy} {
		t.Run(string(d), func(t *testing.T) {
			dir := t.TempDir()
			st, err := Open(dir)
			if err != nil {
				t.Fatal(err)
			}
			defer st.Close()

			samples := synth.Generate(synth.Options{
				Metric: "cpu", Labels: labelsOf("h"), IDPrefix: string(d),
				Start: start, End: end, Density: d, Seed: 42,
			})
			if len(samples) == 0 {
				t.Fatal("合成数据为空")
			}
			rep, err := st.Ingest(samples)
			if err != nil {
				t.Fatal(err)
			}
			if rep.Inserted != len(samples) || rep.Revised != 0 {
				t.Fatalf("摄入报告异常: %+v", rep)
			}
			sk := samples[0].Key()
			assertLayersMatchRaw(t, st, sk, start, end)

			// 空桶可观测性：非 dense 模式下查询范围必然出现空分钟桶。
			pts := st.RollupPoints(string(sk), model.MinuteWindow, start, end, true)
			var empties, nonEmpty int
			for _, p := range pts {
				if p.Count == 0 {
					empties++
				} else {
					nonEmpty++
				}
			}
			if empties == 0 && d != synth.Dense {
				t.Fatalf("密度 %s 期望出现空分钟桶，但没有", d)
			}
			t.Logf("密度=%s 样本=%d 分钟桶 空/非空=%d/%d", d, len(samples), empties, nonEmpty)
		})
	}
}

// TestCrossLayerBoundary 手工构造恰好在分钟/小时边界两侧的样本，
// 验证归属桶正确、不串桶。
func TestCrossLayerBoundary(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	// h1 的最后一秒 与 h2 的第一秒；m1 的最后一秒 与 m2 的第一秒。
	s1 := sample("a", base+3599, 10) // 01:59:59
	s2 := sample("b", base+3600, 20) // 02:00:00
	s3 := sample("c", base+60, 30)   // 01:01:00
	s4 := sample("d", base+59, 40)   // 01:00:59
	rep, err := st.Ingest([]model.Sample{s1, s2, s3, s4})
	if err != nil || rep.Inserted != 4 {
		t.Fatalf("摄入失败: %+v err=%v", rep, err)
	}
	sk := s1.Key()

	mins := st.RollupPoints(string(sk), model.MinuteWindow, base, base+3600, false)
	wantMinStarts := map[int64]float64{
		base:        40, // 01:00 桶只有 d
		base + 60:   30, // 01:01 桶只有 c
		base + 3540: 10, // 01:59 桶只有 a
		base + 3600: 20, // 02:00 桶只有 b
	}
	if len(mins) != 4 {
		t.Fatalf("分钟桶数=%d 期望 4: %+v", len(mins), mins)
	}
	for _, p := range mins {
		if v, ok := wantMinStarts[p.Ts]; !ok || p.Sum != v || p.Count != 1 {
			t.Fatalf("分钟桶 %d 异常: %+v", p.Ts, p)
		}
	}

	hours := st.RollupPoints(string(sk), model.HourWindow, base, base+3600, false)
	if len(hours) != 2 {
		t.Fatalf("小时桶数=%d 期望 2", len(hours))
	}
	h0, h1 := hours[0], hours[1]
	if h0.Ts != base || h0.Count != 3 || math.Abs(h0.Sum-80) > 1e-9 {
		t.Fatalf("h1 桶错误: %+v", h0)
	}
	if h1.Ts != base+3600 || h1.Count != 1 || h1.Sum != 20 {
		t.Fatalf("h2 桶错误: %+v", h1)
	}
	assertLayersMatchRaw(t, st, sk, base, base+7200-1)
}

// TestLateRevisionSameBucket 迟到样本修正同一桶内的值：
// sum/min/max/count 全部按原始样本重建，小时层同步更新。
func TestLateRevisionSameBucket(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	rep, _ := st.Ingest([]model.Sample{
		sample("r1", base+10, 5),
		sample("r2", base+20, 8),
		sample("r3", base+30, 100), // 极端值，稍后被订正
	})
	if rep.Inserted != 3 {
		t.Fatal(rep)
	}
	sk := keyOf(sample("", 0, 0))

	hour := st.RollupPoints(string(sk), model.HourWindow, base, base, false)[0]
	if hour.Max != 100 {
		t.Fatalf("订正前 max 应为 100, 得到 %v", hour.Max)
	}

	// 迟到修正：r3 从 100 改为 6（同 ID、同时间戳）。
	rep, err := st.Ingest([]model.Sample{sample("r3", base+30, 6)})
	if err != nil || rep.Revised != 1 || rep.Inserted != 0 {
		t.Fatalf("修订报告错误: %+v err=%v", rep, err)
	}
	min := st.RollupPoints(string(sk), model.MinuteWindow, base, base, false)[0]
	if min.Count != 3 || math.Abs(min.Sum-19) > 1e-9 || min.Min != 5 || min.Max != 8 {
		t.Fatalf("订正后分钟桶错误: count=%d sum=%v min=%v max=%v",
			min.Count, min.Sum, min.Min, min.Max)
	}
	hour = st.RollupPoints(string(sk), model.HourWindow, base, base, false)[0]
	if hour.Count != 3 || math.Abs(hour.Sum-19) > 1e-9 || hour.Max != 8 {
		t.Fatalf("订正后小时桶未正确传播: %+v", hour)
	}
	assertLayersMatchRaw(t, st, sk, base, base+3599)
}

// TestLateRevisionMovesAcrossBoundaries 迟到修正把样本移到其他分钟
// 甚至其他小时：旧桶扣减、新桶增加，两侧都与原始重算一致。
func TestLateRevisionMovesAcrossBoundaries(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	orig := []model.Sample{
		sample("r1", base+10, 1),
		sample("r2", base+20, 2),
		sample("r3", base+3600+5, 3), // 02:00:05
	}
	if _, err := st.Ingest(orig); err != nil {
		t.Fatal(err)
	}
	sk := orig[0].Key()

	// r1 从 01:00:10 移动到 01:02:05（跨分钟，同小时）。
	if _, err := st.Ingest([]model.Sample{sample("r1", base+125, 1)}); err != nil {
		t.Fatal(err)
	}
	assertLayersMatchRaw(t, st, sk, base, base+7200-1)

	// 再把 r2 从 01:xx 移动到 03:30:00（跨小时）。
	if _, err := st.Ingest([]model.Sample{sample("r2", base+2*3600+1800, 2)}); err != nil {
		t.Fatal(err)
	}
	assertLayersMatchRaw(t, st, sk, base, base+3*3600-1)

	// 旧小时桶现在只剩 0 个样本（r1 还在 01 点！r1 仍在 h1），
	// h1 应剩 r1，h2 有 r3，h3 有 r2；逐桶核对。
	hours := st.RollupPoints(string(sk), model.HourWindow, base, base+3*3600-1, false)
	got := map[int64]int64{}
	for _, p := range hours {
		got[p.Ts] = p.Count
	}
	if got[base] != 1 || got[base+3600] != 1 || got[base+7200] != 1 {
		t.Fatalf("跨小时迁移后计数错误: %v", got)
	}
}

// TestRevisionMovesAcrossSeries 修订把样本改到另一组标签（跨序列），
// 原序列与新序列的两个层都要正确。
func TestRevisionMovesAcrossSeries(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	s := model.Sample{ID: "x", Metric: "m", Labels: labelsOf("h1"), Ts: base + 10, Value: 7}
	if _, err := st.Ingest([]model.Sample{s}); err != nil {
		t.Fatal(err)
	}
	oldSK := s.Key()
	s.Labels = labelsOf("h2")
	newSK := s.Key()
	if oldSK == newSK {
		t.Fatal("测试前置错误：序列键应不同")
	}
	if _, err := st.Ingest([]model.Sample{s}); err != nil {
		t.Fatal(err)
	}
	if len(st.MatchSeries("m", nil)) != 2 {
		t.Fatalf("应存在两条序列")
	}
	if pts := st.RollupPoints(string(oldSK), model.MinuteWindow, base, base, false); len(pts) != 0 {
		t.Fatalf("原序列分钟桶应已清空，实际 %+v", pts)
	}
	if pts := st.RollupPoints(string(newSK), model.MinuteWindow, base, base, false); len(pts) != 1 || pts[0].Sum != 7 {
		t.Fatalf("新序列分钟桶错误: %+v", pts)
	}
	if pts := st.RollupPoints(string(oldSK), model.HourWindow, base, base, false); len(pts) != 0 {
		t.Fatalf("原序列小时桶应已清空，实际 %+v", pts)
	}
}

// TestDeletePropagation 删除样本后，分钟桶按剩余原始样本重建，
// 小时桶重新折叠；删掉桶内唯一样本会留下空洞。
func TestDeletePropagation(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	samples := []model.Sample{
		sample("d1", base+5, 50),
		sample("d2", base+15, -9), // 分钟内最小值
		sample("d3", base+65, 12), // 下一分钟的唯一样本
	}
	if _, err := st.Ingest(samples); err != nil {
		t.Fatal(err)
	}
	sk := samples[0].Key()

	deleted, missing, err := st.Delete([]string{"d2", "nope"})
	if err != nil || deleted != 1 || len(missing) != 1 {
		t.Fatalf("删除结果错误: deleted=%d missing=%v err=%v", deleted, missing, err)
	}
	b0 := st.RollupPoints(string(sk), model.MinuteWindow, base, base, false)[0]
	if b0.Count != 1 || b0.Min != 50 || b0.Max != 50 {
		t.Fatalf("删除后分钟桶 min 应重算为 50: %+v", b0)
	}
	// d3 所在分钟仍在；d2 删除不影响它。
	b1 := st.RollupPoints(string(sk), model.MinuteWindow, base+60, base+60, false)
	if len(b1) != 1 || b1[0].Count != 1 {
		t.Fatalf("下一分钟桶错误: %+v", b1)
	}
	assertLayersMatchRaw(t, st, sk, base, base+120)

	// 再删掉 d3，其分钟桶应消失，小时桶 count 减少。
	if _, _, err := st.Delete([]string{"d3"}); err != nil {
		t.Fatal(err)
	}
	if b1 := st.RollupPoints(string(sk), model.MinuteWindow, base+60, base+60, false); len(b1) != 0 {
		t.Fatalf("删除唯一样本后分钟桶应消失（产生空洞）: %+v", b1)
	}
	h := st.RollupPoints(string(sk), model.HourWindow, base, base, false)[0]
	if h.Count != 1 || h.Sum != 50 {
		t.Fatalf("小时桶未随删除传播: %+v", h)
	}
	// fill=zero 时空洞显式可见。
	filled := st.RollupPoints(string(sk), model.MinuteWindow, base, base+120, true)
	if len(filled) != 3 {
		t.Fatalf("补齐后应有 3 个分钟桶, 实际 %d", len(filled))
	}
	for i, wantCount := range []int64{1, 0, 0} {
		if filled[i].Count != wantCount {
			t.Fatalf("补齐桶[%d] count=%d 期望 %d", i, filled[i].Count, wantCount)
		}
	}

	// 已删除 ID 不可复用。
	rep, _ := st.Ingest([]model.Sample{sample("d2", base+15, 1)})
	if len(rep.Rejected) != 1 {
		t.Fatalf("已删除 ID 应被拒绝, 报告: %+v", rep)
	}
}

// TestIncrementalBatchIngest 分多批、乱序摄入（迟到数据）后，
// 结果仍与一次性重算一致。
func TestIncrementalBatchIngest(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	var all []model.Sample
	for i := 0; i < 400; i++ {
		all = append(all, sample(fmt.Sprintf("s%03d", i),
			base+int64(i%250)*7, float64(i%13)))
	}
	// 乱序分三批。
	batches := [][]model.Sample{all[200:], all[:100], all[100:200]}
	for _, b := range batches {
		if _, err := st.Ingest(b); err != nil {
			t.Fatal(err)
		}
	}
	sk := all[0].Key()
	assertLayersMatchRaw(t, st, sk, base, base+250*7)

	// 重复投递（完全相同）幂等。
	rep, err := st.Ingest(all[:50])
	if err != nil || rep.Duplicate != 50 {
		t.Fatalf("幂等投递错误: %+v err=%v", rep, err)
	}
}

// TestEvictKeepsAggregatesLosesRaw 验证有损保留窗口：
// 驱逐后聚合层保留可查，但原始细节永久丢失且不可再精确订正。
func TestEvictKeepsAggregatesLosesRaw(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	samples := []model.Sample{
		sample("old1", base+0, 1),
		sample("old2", base+30, 3),
		sample("new1", base+3600+10, 9),
	}
	if _, err := st.Ingest(samples); err != nil {
		t.Fatal(err)
	}
	sk := samples[0].Key()
	before := st.RollupPoints(string(sk), model.HourWindow, base, base, false)[0]

	res, err := st.Evict(base + 3600) // 驱逐 02:00 之前的全部原始样本
	if err != nil || res.Evicted != 2 {
		t.Fatalf("驱逐结果错误: %+v err=%v", res, err)
	}

	// 1) 原始样本查不到了。
	if raw := st.RawPoints(string(sk), base, base+3599); len(raw) != 0 {
		t.Fatalf("旧原始样本应已驱逐, 实际 %d 条", len(raw))
	}
	// 2) 分钟/小时聚合桶原样保留（压缩成果还在）。
	after := st.RollupPoints(string(sk), model.HourWindow, base, base, false)[0]
	if after.Count != before.Count || after.Sum != before.Sum ||
		after.Min != before.Min || after.Max != before.Max {
		t.Fatalf("驱逐后聚合桶被改变: before=%+v after=%+v", before, after)
	}
	// 3) 从原始重算已无法复现旧桶——不可恢复细节的直接证据。
	recomp := st.RecomputeFromRaw(string(sk), model.HourWindow, base, base, true)
	if recomp[0].Count != 0 {
		t.Fatalf("驱逐后原始重算应为空桶, 实际 count=%d", recomp[0].Count)
	}
	// 4) 被驱逐 ID 既不能修订也不能复用。
	rep, _ := st.Ingest([]model.Sample{sample("old1", base, 999)})
	if len(rep.Rejected) != 1 {
		t.Fatalf("被驱逐 ID 的修订应被拒绝: %+v", rep)
	}
	// 5) 新数据仍正常聚合，且与新小时桶中的旧数据共存。
	if _, err := st.Ingest([]model.Sample{sample("new2", base+3600+20, 11)}); err != nil {
		t.Fatal(err)
	}
	h2 := st.RollupPoints(string(sk), model.HourWindow, base+3600, base+3600, false)[0]
	if h2.Count != 2 || math.Abs(h2.Sum-20) > 1e-9 {
		t.Fatalf("驱逐后新写入聚合错误: %+v", h2)
	}
}

// TestPersistenceSnapshot 正常关闭（带快照）后重开，状态完全恢复。
func TestPersistenceSnapshot(t *testing.T) {
	dir := t.TempDir()
	sk := model.SeriesKey(model.SeriesID("m", labelsOf("h")))

	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Ingest([]model.Sample{
		sample("p1", base+10, 4),
		sample("p2", base+3700, 6),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Ingest([]model.Sample{sample("p1", base+10, 40)}); err != nil {
		t.Fatal(err) // 修订
	}
	if _, _, err := st.Delete([]string{"p2"}); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()

	assertLayersMatchRaw(t, st2, sk, base, base+7200-1)
	pts := st2.RawPoints(string(sk), base, base+7200)
	if len(pts) != 1 || pts[0].Value != 40 {
		t.Fatalf("重开后原始数据错误: %+v", pts)
	}
	// forgotten 集合持久化：p2 仍被拒绝。
	rep, _ := st2.Ingest([]model.Sample{sample("p2", base+3700, 1)})
	if len(rep.Rejected) != 1 {
		t.Fatalf("重开后已删除 ID 应仍被拒绝: %+v", rep)
	}
}

// TestPersistenceOplogReplay 模拟崩溃：只写了 oplog、没有快照就重开。
func TestPersistenceOplogReplay(t *testing.T) {
	dir := t.TempDir()
	sk := model.SeriesKey(model.SeriesID("m", labelsOf("h")))

	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	samples := make([]model.Sample, 0, 120)
	for i := 0; i < 120; i++ { // 跨两个小时
		samples = append(samples, sample(fmt.Sprintf("c%03d", i),
			base+int64(i)*90, float64(i%7)))
	}
	if _, err := st.Ingest(samples); err != nil {
		t.Fatal(err)
	}
	// 修订 + 删除都进 oplog，但刻意不 Close / 不 SaveSnapshot。
	if _, err := st.Ingest([]model.Sample{sample("c000", base, 99)}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := st.Delete([]string{"c001"}); err != nil {
		t.Fatal(err)
	}

	// 直接用新实例重放（旧实例不再使用，文件内容已随每条写入 fsync）。
	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	assertLayersMatchRaw(t, st2, sk, base, base+3*3600)
	first := st2.RawPoints(string(sk), base, base)[0]
	if first.Value != 99 {
		t.Fatalf("重放后修订丢失: %+v", first)
	}
}

// TestPersistenceSnapshotPlusOplog 快照后新增的操作经 oplog 重放衔接。
func TestPersistenceSnapshotPlusOplog(t *testing.T) {
	dir := t.TempDir()
	sk := model.SeriesKey(model.SeriesID("m", labelsOf("h")))

	st, _ := Open(dir)
	if _, err := st.Ingest([]model.Sample{sample("a", base, 1)}); err != nil {
		t.Fatal(err)
	}
	if err := st.SaveSnapshot(); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Ingest([]model.Sample{sample("b", base+3600, 2)}); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil { // Close 再次快照，包含 b
		t.Fatal(err)
	}
	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	assertLayersMatchRaw(t, st2, sk, base, base+3600)
}

// TestValidationRejections 非法样本被拒绝且不影响其余样本。
func TestValidationRejections(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	bad := []model.Sample{
		{ID: "1", Metric: "", Ts: base, Value: 1},
		{ID: "2", Metric: "m", Labels: labelsOf("h"), Ts: base, Value: math.NaN()},
		{ID: "3", Metric: "m", Labels: labelsOf("h"), Ts: base, Value: math.Inf(1)},
		{ID: "4", Metric: "m", Labels: labelsOf("h"), Ts: base, Value: 5},
	}
	rep, err := st.Ingest(bad)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Inserted != 1 || len(rep.Rejected) != 3 {
		t.Fatalf("拒绝计数错误: %+v", rep)
	}
}

// TestHourAlwaysFoldedFromMinutes 不变量：小时桶等于其 60 个分钟桶
// 的折叠（而非直接由样本增量更新的结果“碰巧正确”）——在多轮
// 修订/删除/迁移后逐小时验证。
func TestHourAlwaysFoldedFromMinutes(t *testing.T) {
	st, _ := Open(t.TempDir())
	defer st.Close()

	sk := model.SeriesKey(model.SeriesID("m", labelsOf("h")))
	var all []model.Sample
	for i := 0; i < 300; i++ {
		all = append(all, sample(fmt.Sprintf("f%03d", i),
			base+int64(i%200)*45, float64((i*37)%101)))
	}
	if _, err := st.Ingest(all); err != nil {
		t.Fatal(err)
	}
	// 一连串修订与删除。
	for _, i := range []int{0, 59, 60, 120, 199} {
		s := all[i]
		s.Value = float64(i) + 0.5
		if _, err := st.Ingest([]model.Sample{s}); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := st.Delete([]string{all[70].ID, all[71].ID}); err != nil {
		t.Fatal(err)
	}
	assertHoursFolded(t, st, sk)
}

func assertHoursFolded(t *testing.T, st *Store, sk model.SeriesKey) {
	t.Helper()
	idx := st.series[sk]
	hourStarts := make([]int64, 0, len(idx.hours))
	for h := range idx.hours {
		hourStarts = append(hourStarts, h)
	}
	sort.Slice(hourStarts, func(i, j int) bool { return hourStarts[i] < hourStarts[j] })
	for _, h := range hourStarts {
		var folded agg.Bucket
		for m := h; m < h+3600; m += 60 {
			if mb, ok := idx.minutes[m]; ok {
				folded.Merge(*mb)
			}
		}
		hb := *idx.hours[h]
		if !folded.AlmostEqual(hb, 1e-9) {
			t.Fatalf("小时桶 %d 与分钟折叠不一致: stored=%+v folded=%+v", h, hb, folded)
		}
	}
}
