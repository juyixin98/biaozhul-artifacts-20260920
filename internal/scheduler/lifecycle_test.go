package scheduler

import (
	"testing"
	"time"

	"resourcebooking/internal/clock"
)

func TestAdvanceLifecycle(t *testing.T) {
	fc := clock.NewFake(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	sch := New(NewEventLog(fc))
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, sch, Request{ID: "a", Resource: "r", Interval: Interval{10, 20}, Demand: Demand{1}})
	mustReserve(t, sch, Request{ID: "b", Resource: "r", Interval: Interval{20, 30}, Demand: Demand{1}})

	// 推进到 10：a 开始；b 仍 pending。容量 1 在 [10,20) 仍被 a 占用。
	res := sch.Advance(10)
	if len(res.Started) != 1 || res.Started[0] != "a" {
		t.Fatalf("期望只有 a 开始，得到 %+v", res)
	}
	if got, _ := sch.Get("a"); got.Status != StatusRunning {
		t.Fatalf("a 应为 running，得到 %s", got.Status)
	}
	if _, err := sch.Reserve(Request{Resource: "r", Interval: Interval{10, 20}, Demand: Demand{1}}); err == nil {
		t.Fatal("running 预约仍应占容量")
	}
	// 相邻新区间 [20,...) 在 a 结束点放行——即使 b 在那里开始，
	// [20,30) 已被 b 占满，所以 [20,21) 冲突，但 [30,31) 可行。
	if _, err := sch.Reserve(Request{Resource: "r", Interval: Interval{20, 21}, Demand: Demand{1}}); err == nil {
		t.Fatal("b 占据 [20,30)，该区间应冲突")
	}

	// 推进到 20：a 完成并释放；b 开始（同一拍内先开始后结束）。
	res = sch.Advance(20)
	if len(res.Completed) != 1 || res.Completed[0] != "a" {
		t.Fatalf("期望 a 完成，得到 %+v", res)
	}
	if len(res.Started) != 1 || res.Started[0] != "b" {
		t.Fatalf("期望 b 开始，得到 %+v", res)
	}
	if got, _ := sch.Get("a"); got.Status != StatusCompleted {
		t.Fatalf("a 应为 completed，得到 %s", got.Status)
	}
	// a 的容量已释放：[10,20) 可再预约。
	rebook := mustReserve(t, sch, Request{Resource: "r", Interval: Interval{10, 20}, Demand: Demand{1}})

	// 越过 b 的结束点：b 与 rebook（区间 [10,20)，在 tick=20
	// 创建时起点已过、状态仍 pending）都应在本次推进中完成。
	res = sch.Advance(100)
	wantCompleted := map[string]bool{"b": true, rebook.ID: true}
	if len(res.Completed) != len(wantCompleted) {
		t.Fatalf("期望完成 %v，得到 %+v", wantCompleted, res.Completed)
	}
	for _, id := range res.Completed {
		if !wantCompleted[id] {
			t.Fatalf("意外完成的预约: %s", id)
		}
	}
	if got, _ := sch.Get("b"); got.Status != StatusCompleted {
		t.Fatalf("b 应为 completed，得到 %s", got.Status)
	}
}

func TestMarkFailedReleasesCapacity(t *testing.T) {
	sch := New(nil)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	r1 := mustReserve(t, sch, Request{ID: "a", Resource: "r", Interval: Interval{0, 100}, Demand: Demand{1}})
	sch.Advance(1)
	if _, err := sch.MarkFailed(r1.ID, "boom"); err != nil {
		t.Fatal(err)
	}
	if got, _ := sch.Get("a"); got.Status != StatusFailed {
		t.Fatalf("期望 failed，得到 %s", got.Status)
	}
	// 容量释放。
	mustReserve(t, sch, Request{Resource: "r", Interval: Interval{0, 100}, Demand: Demand{1}})
	// 重复报告必须拒绝。
	if _, err := sch.MarkFailed(r1.ID, "again"); err == nil {
		t.Fatal("终态预约不能再次标记失败")
	}
}

func TestStructuredEvents(t *testing.T) {
	fc := clock.NewFake(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	log := NewEventLog(fc)
	sch := New(log)

	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, sch, Request{ID: "a", Resource: "r", Interval: Interval{0, 5}, Demand: Demand{1}})
	if _, err := sch.Reserve(Request{ID: "b", Resource: "r", Interval: Interval{0, 5}, Demand: Demand{1}}); err == nil {
		t.Fatal("b 应冲突")
	}
	sch.Cancel("a", 0)

	evs := log.Events()
	wantTypes := []string{
		EventResourceAdded,
		EventReservationCreated,
		EventReservationRejected,
		EventReservationCancelled,
	}
	if len(evs) < len(wantTypes) {
		t.Fatalf("事件数不足: %+v", evs)
	}
	for i, wt := range wantTypes {
		if evs[i].Type != wt {
			t.Fatalf("事件 %d 期望 %s，得到 %s", i, wt, evs[i].Type)
		}
		if evs[i].Seq != int64(i+1) {
			t.Fatalf("事件序号应从 1 单调递增，第 %d 条 seq=%d", i, evs[i].Seq)
		}
		if evs[i].At.IsZero() {
			t.Fatalf("事件 %s 缺少时间戳", wt)
		}
	}
	// 拒绝事件必须带结构化错误负载。
	rj := evs[2].Detail.(RejectedDetail)
	if rj.Error == nil || rj.Error.Code != CodeConflict {
		t.Fatalf("拒绝事件负载不正确: %+v", rj)
	}
	if rj.RequestID != "b" {
		t.Fatalf("拒绝事件应携带请求 ID b，得到 %q", rj.RequestID)
	}
}

func TestBatchEventsAtomic(t *testing.T) {
	log := NewEventLog(nil)
	sch := New(log)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	// 成功批次：一条 batch_created。
	_, err := sch.Batch([]BatchItem{
		{Fixed: &Request{ID: "x1", Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}}},
		{Fixed: &Request{ID: "x2", Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	// 失败批次：一条 batch_rejected，且不得出现为其条目补发的 created。
	_, err = sch.Batch([]BatchItem{
		{Fixed: &Request{ID: "y1", Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}}},
		{Fixed: &Request{ID: "y2", Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}}},
	})
	if err == nil {
		t.Fatal("批次应失败")
	}
	var sawReject, sawYCreated bool
	for _, e := range log.Events() {
		if e.Type == EventReservationBatchRejected {
			sawReject = true
			d := e.Detail.(BatchRejectedDetail)
			if len(d.Items) != 2 {
				t.Fatalf("期望 2 个条目错误，得到 %d", len(d.Items))
			}
		}
		if e.Type == EventReservationCreated {
			if d, ok := e.Detail.(*Reservation); ok && (d.ID == "y1" || d.ID == "y2") {
				sawYCreated = true
			}
		}
	}
	if !sawReject {
		t.Fatal("缺少 batch_rejected 事件")
	}
	if sawYCreated {
		t.Fatal("失败批次不得产生 created 事件")
	}
}

func TestEventSubscribe(t *testing.T) {
	log := NewEventLog(nil)
	sch := New(log)
	id, ch := log.Subscribe(4)
	defer log.Unsubscribe(id)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	select {
	case e := <-ch:
		if e.Type != EventResourceAdded {
			t.Fatalf("订阅收到错误事件: %s", e.Type)
		}
	case <-time.After(time.Second):
		t.Fatal("未收到订阅事件")
	}
}
