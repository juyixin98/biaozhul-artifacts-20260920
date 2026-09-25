package scheduler

import (
	"errors"
	"testing"
)

func newTestScheduler(t *testing.T) *Scheduler {
	t.Helper()
	return New(nil)
}

func mustReserve(t *testing.T, s *Scheduler, r Request) *Reservation {
	t.Helper()
	got, err := s.Reserve(r)
	if err != nil {
		t.Fatalf("预约 %+v 意外失败: %v", r, err)
	}
	return got
}

func expectConflict(t *testing.T, err error) *Error {
	t.Helper()
	if err == nil {
		t.Fatal("期望冲突错误，但预约成功了")
	}
	se := AsError(err)
	if se == nil {
		t.Fatalf("期望结构化 *Error，得到 %T: %v", err, err)
	}
	if se.Code != CodeConflict {
		t.Fatalf("期望 code=%s，得到 %s (%s)", CodeConflict, se.Code, se.Message)
	}
	return se
}

// TestAdjacentIntervalsDoNotConflict 验收点：相邻区间不冲突，
// 且相邻两笔可以打满同一容量。
func TestAdjacentIntervalsDoNotConflict(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "room", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, s, Request{Resource: "room", Interval: Interval{1, 3}, Demand: Demand{1}})
	mustReserve(t, s, Request{Resource: "room", Interval: Interval{3, 5}, Demand: Demand{1}})

	// 但跨越 3 的区间必须冲突（说明不是“随便放都行”）。
	_, err := s.Reserve(Request{Resource: "room", Interval: Interval{2, 4}, Demand: Demand{1}})
	expectConflict(t, err)
}

// TestZeroCapacityDimension 验收点：容量为 0 的维度不接受任何正需求；
// 零需求可以预约；其它维度照常工作。
func TestZeroCapacityDimension(t *testing.T) {
	s := newTestScheduler(t)
	// 两维：房间数 1，附加服务（如“专车”）容量 0。
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1, 0}}); err != nil {
		t.Fatal(err)
	}
	// 全零需求：允许（不占用任何容量），且可以同时多笔。
	mustReserve(t, s, Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{0, 0}})
	mustReserve(t, s, Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{0, 0}})

	// 第二维需求 1 > 容量 0：单条即被拒。
	_, err := s.Reserve(Request{Resource: "r", Interval: Interval{0, 1}, Demand: Demand{0, 1}})
	if err == nil {
		t.Fatal("零容量维度上的正需求必须被拒绝")
	}
	if AsError(err).Code != CodeInvalidRequest {
		t.Fatalf("单条超容量应是 invalid_request，得到 %v", err)
	}

	// 第一维正、第二维 0：正常；同区间再放一笔则按第一维冲突。
	mustReserve(t, s, Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1, 0}})
	_, err = s.Reserve(Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1, 0}})
	expectConflict(t, err)

	// 相邻区间两笔各需 1，第一维也必须放行。
	mustReserve(t, s, Request{Resource: "r", Interval: Interval{10, 20}, Demand: Demand{1, 0}})
}

// TestMultiDimensionalCapacity 多维容量逐维约束。
func TestMultiDimensionalCapacity(t *testing.T) {
	s := newTestScheduler(t)
	// [会议室间数, 席位数] = [2, 6]
	if _, err := s.AddResource(Resource{ID: "venue", Capacity: []int64{2, 6}}); err != nil {
		t.Fatal(err)
	}
	// [1,4] + [1,3] = [2,7]：第二维超，第一维不超。
	mustReserve(t, s, Request{ID: "a", Resource: "venue", Interval: Interval{0, 5}, Demand: Demand{1, 4}})
	_, err := s.Reserve(Request{Resource: "venue", Interval: Interval{0, 5}, Demand: Demand{1, 3}})
	se := expectConflict(t, err)
	foundDim1 := false
	for _, v := range se.Violations {
		if v.Dimension == 1 && v.Load == 7 && v.Capacity == 6 {
			foundDim1 = true
		}
		if v.Dimension == 0 {
			t.Fatal("第一维负载 2 未超容量 2，不应报告违规")
		}
	}
	if !foundDim1 {
		t.Fatalf("期望第二维 [0,5) 负载 7 超 6 的违规，得到 %+v", se.Violations)
	}
	if len(se.Conflicts) != 1 || se.Conflicts[0] != "a" {
		t.Fatalf("期望冲突预约 [a]，得到 %v", se.Conflicts)
	}
}

// TestEarliestFeasible 最早可行位置的基本语义。
func TestEarliestFeasible(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, s, Request{ID: "busy", Resource: "r", Interval: Interval{2, 5}, Demand: Demand{1}})

	// 窗口 [0,10)，时长 3：[0,3) 与 [2,5) 撞，最早可行应跳到 5（占用结束点）。
	start, ok, err := s.EarliestFeasible("r", Interval{0, 10}, 3, 0, Demand{1})
	if err != nil || !ok {
		t.Fatalf("期望可行，得到 ok=%v err=%v", ok, err)
	}
	if start != 5 {
		t.Fatalf("期望最早起点 5，得到 %d", start)
	}

	// earliest 约束：不得早于 6。
	start, ok, _ = s.EarliestFeasible("r", Interval{0, 10}, 3, 6, Demand{1})
	if !ok || start != 6 {
		t.Fatalf("期望起点 6，得到 %d ok=%v", start, ok)
	}

	// 窗口放不下：不可行但非错误。
	_, ok, err = s.EarliestFeasible("r", Interval{0, 10}, 6, 0, Demand{1})
	if ok || err != nil {
		t.Fatalf("时长 6 与 [2,5) 冲突后无 6 格连续空位，应 ok=false，得到 ok=%v err=%v", ok, err)
	}

	// 相邻可行：时长 2，[0,2) 可行。
	start, ok, _ = s.EarliestFeasible("r", Interval{0, 10}, 2, 0, Demand{1})
	if !ok || start != 0 {
		t.Fatalf("期望起点 0，得到 %d ok=%v", start, ok)
	}
}

// TestBatchAtomicOnPartialConflict 验收点：批次中只要一条冲突，
// 整批不得有任何一条落地。
func TestBatchAtomicOnPartialConflict(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, s, Request{ID: "existing", Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1}})

	before := len(s.List(ListFilter{}))
	items := []BatchItem{
		{Fixed: &Request{ID: "ok1", Resource: "r", Interval: Interval{10, 11}, Demand: Demand{1}}},
		// 与 existing 冲突。
		{Fixed: &Request{ID: "bad", Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}}},
		// 本条单独可行，但必须随整批回滚。
		{Fixed: &Request{ID: "ok2", Resource: "r", Interval: Interval{11, 12}, Demand: Demand{1}}},
	}
	_, err := s.Batch(items)
	if err == nil {
		t.Fatal("批次含冲突条目，必须失败")
	}
	var be *BatchError
	if !errors.As(err, &be) {
		t.Fatalf("期望 *BatchError，得到 %T", err)
	}
	if len(be.Items) != 1 || be.Items[0].Index != 1 || be.Items[0].ID != "bad" {
		t.Fatalf("期望仅第 2 条（index=1,id=bad）报错，得到 %+v", be.Items)
	}
	after := len(s.List(ListFilter{}))
	if before != after {
		t.Fatalf("失败批次不得部分落地：落地数量 %d -> %d", before, after)
	}
	for _, id := range []string{"ok1", "bad", "ok2"} {
		if _, err := s.Get(id); err == nil {
			t.Fatalf("预约 %s 不应存在于失败批次之后", id)
		}
	}
}

// TestBatchInternalConflicts 批次内条目互相冲突也必须整批拒绝。
func TestBatchInternalConflicts(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	items := []BatchItem{
		{Fixed: &Request{ID: "a", Resource: "r", Interval: Interval{0, 5}, Demand: Demand{1}}},
		{Fixed: &Request{ID: "b", Resource: "r", Interval: Interval{3, 8}, Demand: Demand{1}}},
	}
	if _, err := s.Batch(items); err == nil {
		t.Fatal("批次内重叠条目必须失败")
	}
	if _, err := s.Get("a"); err == nil {
		t.Fatal("失败批次中的 a 不应落地")
	}
}

// TestBatchAdjacentSucceeds 批次内相邻条目打满容量应当成功。
func TestBatchAdjacentSucceeds(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	items := []BatchItem{
		{Fixed: &Request{ID: "a", Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}}},
		{Fixed: &Request{ID: "b", Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}}},
		{Fixed: &Request{ID: "c", Resource: "r", Interval: Interval{2, 3}, Demand: Demand{1}}},
	}
	got, err := s.Batch(items)
	if err != nil {
		t.Fatalf("相邻批次应成功: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("期望落地 3 条，得到 %d", len(got))
	}
}

// TestBatchAutoPlacement 自动放置条目在批次内顺序占位。
func TestBatchAutoPlacement(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	// 已占用 [0,2)，两笔自动放置各 2 小时：应分别落在 2 与 4。
	mustReserve(t, s, Request{ID: "seed", Resource: "r", Interval: Interval{0, 2}, Demand: Demand{1}})
	items := []BatchItem{
		{Place: &PlacementRequest{ID: "p1", Resource: "r", Window: Interval{0, 10}, Duration: 2, Demand: Demand{1}}},
		{Place: &PlacementRequest{ID: "p2", Resource: "r", Window: Interval{0, 10}, Duration: 2, Demand: Demand{1}}},
	}
	got, err := s.Batch(items)
	if err != nil {
		t.Fatalf("自动放置批次应成功: %v", err)
	}
	if got[0].Interval != (Interval{2, 4}) || !got[0].AutoPlaced {
		t.Fatalf("p1 期望 [2,4) 自动放置，得到 %+v", got[0])
	}
	if got[1].Interval != (Interval{4, 6}) {
		t.Fatalf("p2 期望 [4,6)，得到 %+v", got[1].Interval)
	}
}

// TestBatchMixedAutoPlacementConflict 放置条目与固定条目冲突时整批回滚。
func TestBatchMixedAutoPlacementConflict(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	// 容量 1，窗口 [0,5) 内已有 [2,3)；
	// fixed 占 [3,6)（与窗口末端重叠），place 要长度 4：
	// 相对 seed+fixed 没有任何 4 格空位 -> 整批拒绝。
	mustReserve(t, s, Request{ID: "seed", Resource: "r", Interval: Interval{2, 3}, Demand: Demand{1}})
	items := []BatchItem{
		{Fixed: &Request{ID: "f1", Resource: "r", Interval: Interval{3, 6}, Demand: Demand{1}}},
		{Place: &PlacementRequest{ID: "p1", Resource: "r", Window: Interval{0, 6}, Duration: 4, Demand: Demand{1}}},
	}
	if _, err := s.Batch(items); err == nil {
		t.Fatal("放置条目无空位时批次必须失败")
	}
	if _, err := s.Get("f1"); err == nil {
		t.Fatal("f1 不得部分落地")
	}
}

// TestCancelReleasesCapacity 取消后容量立即可供相邻/重叠预约使用。
func TestCancelReleasesCapacity(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	r1 := mustReserve(t, s, Request{ID: "r1", Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1}})
	if _, err := s.Reserve(Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1}}); err == nil {
		t.Fatal("取消前重叠预约应失败")
	}
	if _, err := s.Cancel(r1.ID, 0); err != nil {
		t.Fatalf("取消失败: %v", err)
	}
	mustReserve(t, s, Request{Resource: "r", Interval: Interval{0, 10}, Demand: Demand{1}})
}

// TestValidationErrors 各类非法输入。
func TestValidationErrors(t *testing.T) {
	s := newTestScheduler(t)
	if _, err := s.AddResource(Resource{ID: "", Capacity: []int64{1}}); err == nil {
		t.Fatal("空资源 ID 必须拒绝")
	}
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{-1}}); err == nil {
		t.Fatal("负容量必须拒绝")
	}
	if _, err := s.AddResource(Resource{ID: "r"}); err == nil {
		t.Fatal("零维容量必须拒绝")
	}
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Reserve(Request{Resource: "r", Interval: Interval{0, 1}, Demand: Demand{}}); err == nil {
		t.Fatal("维度不匹配必须拒绝")
	}
	if _, err := s.Reserve(Request{Resource: "r", Interval: Interval{0, 1}, Demand: Demand{-1}}); err == nil {
		t.Fatal("负需求必须拒绝")
	}
	if _, err := s.Reserve(Request{Resource: "missing", Interval: Interval{0, 1}, Demand: Demand{1}}); err == nil {
		t.Fatal("不存在的资源必须拒绝")
	}
	if _, err := s.Reserve(Request{Resource: "r", Interval: Interval{2, 1}, Demand: Demand{1}}); err == nil {
		t.Fatal("非法区间必须拒绝")
	}
}
