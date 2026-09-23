package engine

import (
	"testing"
)

type recvEvent struct {
	at   int64
	from NodeID
	what string
}

type testNode struct {
	Base
	eng   *Engine
	got   []recvEvent
	timer int
}

func newTestNode(eng *Engine, id NodeID) *testNode {
	n := &testNode{eng: eng}
	n.Base.NodeIDValue = id
	eng.Register(n)
	return n
}

func (n *testNode) HandleMessage(now int64, from NodeID, payload any) {
	n.got = append(n.got, recvEvent{at: now, from: from, what: payload.(string)})
}
func (n *testNode) OnRestart(now int64) { n.MarkUp() }
func (n *testNode) Crash() {
	n.got = nil
	n.MarkDown()
}

func TestDeterministicNetworkAndQueue(t *testing.T) {
	run := func() []Record {
		var trace []Record
		eng := New(123, func(r Record) { trace = append(trace, r) },
			NetConfig{BaseDelay: 2, Jitter: 5, Loss: 0.4, Duplicate: 0.3})
		a := newTestNode(eng, "a")
		b := newTestNode(eng, "b")
		for i := 0; i < 20; i++ {
			i := i
			eng.ScheduleFunc(0, func(now int64) {
				eng.Send(a.ID(), b.ID(), string(rune('A'+i%4)))
			})
		}
		eng.Run(100)
		if len(b.got) == 0 {
			t.Fatal("expected some deliveries")
		}
		return trace
	}
	t1 := run()
	t2 := run()
	if len(t1) != len(t2) {
		t.Fatalf("trace length differs: %d vs %d", len(t1), len(t2))
	}
	for i := range t1 {
		if t1[i] != t2[i] {
			t.Fatalf("trace differs at %d:\n%+v\n%+v", i, t1[i], t2[i])
		}
	}
}

// Messages to a crashed node are dropped; stale pre-crash timers never fire.
func TestCrashDropsMessagesAndTimers(t *testing.T) {
	var trace []Record
	eng := New(1, func(r Record) { trace = append(trace, r) },
		NetConfig{BaseDelay: 1, Jitter: 0})
	a := newTestNode(eng, "a")
	b := newTestNode(eng, "b")
	_ = a

	// Timer armed on b before the crash; it must not fire after crash.
	fired := false
	eng.ScheduleFunc(1, func(now int64) {
		After(eng, &b.Base, 5, func(now int64) { fired = true })
	})
	// b crashes at tick 3 and stays down.
	eng.ScheduleFunc(3, func(now int64) { eng.Crash("b", 0) })
	// a sends at tick 4 (delivered tick 5) — must be dropped.
	eng.ScheduleFunc(4, func(now int64) { eng.Send("a", "b", "x") })

	eng.Run(50)
	if fired {
		t.Errorf("stale timer fired across crash")
	}
	if len(b.got) != 0 {
		t.Errorf("crashed node received messages: %+v", b.got)
	}
	foundDrop := false
	for _, r := range trace {
		if r.Kind == "drop" && r.Detail == "to=b type=string reason=node-down" {
			foundDrop = true
		}
	}
	if !foundDrop {
		t.Errorf("expected node-down drop record in trace")
	}
}

// After restart, the node delivers again.
func TestRestartDelivers(t *testing.T) {
	eng := New(1, func(r Record) {}, NetConfig{BaseDelay: 1, Jitter: 0})
	a := newTestNode(eng, "a")
	b := newTestNode(eng, "b")
	_ = a
	eng.ScheduleFunc(1, func(now int64) { eng.Crash("b", 10) })
	eng.ScheduleFunc(5, func(now int64) { eng.Send("a", "b", "while-down") })
	eng.ScheduleFunc(20, func(now int64) { eng.Send("a", "b", "after-up") })
	eng.Run(50)
	if len(b.got) != 1 || b.got[0].what != "after-up" {
		t.Fatalf("got %+v, want only after-up", b.got)
	}
}

// Milestone crash hook: sentinel aborts the handler; code after CrashAt in
// the handler must not execute.
func TestMilestoneHookAbortsHandler(t *testing.T) {
	eng := New(1, func(r Record) {}, NetConfig{BaseDelay: 1, Jitter: 0})
	n := &hookNode{}
	n.Base.NodeIDValue = "h"
	n.eng = eng
	eng.Register(n)

	n.Hook = func(id NodeID, milestone string) bool {
		if milestone == "boom" {
			eng.Crash(id, 0)
			return true
		}
		return false
	}
	panicked := false
	func() {
		defer func() {
			if r := recover(); r != nil {
				panicked = true
			}
		}()
		n.CrashAt("boom")
	}()
	if !panicked {
		t.Fatalf("CrashAt did not unwind the crashing handler")
	}
	if n.afterCrashRan {
		t.Errorf("code after crashing milestone executed")
	}
}

type hookNode struct {
	Base
	eng           *Engine
	afterCrashRan bool
}

func (n *hookNode) HandleMessage(now int64, from NodeID, payload any) {}
func (n *hookNode) OnRestart(now int64)                               {}
func (n *hookNode) Crash()                                            { n.MarkDown() }
