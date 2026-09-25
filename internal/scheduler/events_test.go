package scheduler

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"resourcebooking/internal/clock"
)

func TestJSONLSink(t *testing.T) {
	var buf bytes.Buffer
	fc := clock.NewFake(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	log := NewEventLog(fc)
	log.AddSink(NewJSONLSink(&buf))
	sch := New(log)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, sch, Request{ID: "a", Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}})

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("期望 2 行 JSONL，得到 %d: %q", len(lines), buf.String())
	}
	for i, line := range lines {
		var e Event
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			t.Fatalf("第 %d 行不是合法 JSON: %v", i+1, err)
		}
		if e.Seq != int64(i+1) {
			t.Fatalf("第 %d 行序号错误: %d", i+1, e.Seq)
		}
	}
	if err := log.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

// TestEventsAreImmutableSnapshots 回归：事件负载必须是快照，
// 预约后续的状态迁移（取消、推进生命周期）不得回改历史事件。
func TestEventsAreImmutableSnapshots(t *testing.T) {
	fc := clock.NewFake(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	log := NewEventLog(fc)
	sch := New(log)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	r := mustReserve(t, sch, Request{ID: "a", Resource: "r",
		Interval: Interval{0, 10}, Demand: Demand{1}})

	// 找到 created 事件并记录其快照状态。
	var created Event
	for _, e := range log.Events() {
		if e.Type == EventReservationCreated {
			created = e
		}
	}
	snap := created.Detail.(*Reservation)
	if snap.Status != StatusPending {
		t.Fatalf("创建时快照应为 pending，得到 %s", snap.Status)
	}

	// 取消 + 推进生命周期后，历史事件负载必须保持原样。
	if _, err := sch.Cancel(r.ID, 0); err != nil {
		t.Fatal(err)
	}
	sch.Advance(100)
	if got, _ := sch.Get("a"); got.Status != StatusCancelled {
		t.Fatalf("前置条件：当前记录应为 cancelled，得到 %s", got.Status)
	}
	if snap.Status != StatusPending {
		t.Fatalf("历史 created 事件被回改为 %s，事件不是不可变快照", snap.Status)
	}

	// 资源事件同样应是快照（这里仅验证指针不随返回值变化）。
	for _, e := range log.Events() {
		if e.Type == EventResourceAdded {
			if e.Detail.(*Resource).ID != "r" {
				t.Fatal("资源事件负载异常")
			}
		}
	}
}

func TestEventSince(t *testing.T) {
	log := NewEventLog(nil)
	sch := New(log)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	mustReserve(t, sch, Request{ID: "a", Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}})
	mustReserve(t, sch, Request{ID: "b", Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}})

	if got := log.Since(0); len(got) != 3 {
		t.Fatalf("Since(0) 期望 3 条，得到 %d", len(got))
	}
	if got := log.Since(2); len(got) != 1 || got[0].Type != EventReservationCreated {
		t.Fatalf("Since(2) 期望只剩最后一条 created，得到 %+v", got)
	}
	if got := log.Since(99); len(got) != 0 {
		t.Fatalf("Since(99) 应为空，得到 %d 条", len(got))
	}
}
