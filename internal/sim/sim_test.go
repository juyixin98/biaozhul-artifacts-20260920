package sim_test

import (
	"testing"

	"fencinglease/internal/sim"
)

// probe records every timer/message delivery it observes.
type probe struct {
	id    string
	got   []int64
	times []int64
}

func (p *probe) NodeID() string { return p.id }
func (p *probe) Handle(_ *sim.Env, ev *sim.Event) {
	switch ev.Kind {
	case "timer":
		p.times = append(p.times, ev.Time)
	case "message":
		p.got = append(p.got, ev.Time)
	}
}

// sender emits one message ("ping" by default) each time a "go" timer fires.
type sender struct {
	id, to, method string
	n              int
}

func (s *sender) NodeID() string { return s.id }
func (s *sender) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind == "timer" {
		s.n++
		env.Send(&sim.Message{From: s.id, To: s.to, Method: s.method})
	}
}

func TestTimersFireInTimeOrder(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	p := &probe{id: "p"}
	eng.Register(p)
	eng.Schedule(50, "p", "late", nil)
	eng.Schedule(10, "p", "early", nil)
	eng.Schedule(30, "p", "mid", nil)
	eng.Run(100)
	want := []int64{10, 30, 50}
	if len(p.times) != 3 {
		t.Fatalf("got %d timer fires, want 3", len(p.times))
	}
	for i := range want {
		if p.times[i] != want[i] {
			t.Fatalf("timer order %v, want %v", p.times, want)
		}
	}
}

func TestRNGDeterministicAcrossInstances(t *testing.T) {
	r1, r2 := sim.NewRng(42), sim.NewRng(42)
	for i := 0; i < 1000; i++ {
		if r1.Uint32() != r2.Uint32() {
			t.Fatalf("RNG streams diverge at %d", i)
		}
	}
	r3 := sim.NewRng(43)
	diff := false
	for i := 0; i < 10; i++ {
		if r1.Uint32() != r3.Uint32() {
			diff = true
		}
	}
	if !diff {
		t.Fatal("different seeds produced identical streams")
	}
}

func TestNetworkDropRule(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{Rules: []*sim.Rule{{
		Action: "drop", Match: sim.Match{Method: "ping"},
	}}})
	b := &probe{id: "b"}
	eng.Register(b)
	s := &sender{id: "a", to: "b", method: "ping"}
	eng.Register(s)
	eng.Schedule(0, "a", "go", nil)
	eng.Run(10)
	if len(b.got) != 0 {
		t.Fatalf("dropped message was delivered: %v", b.got)
	}
}

func TestNetworkDelayRule(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{Rules: []*sim.Rule{{
		Action: "delay", Delay: 9, Match: sim.Match{Method: "ping"},
	}}})
	b := &probe{id: "b"}
	eng.Register(b)
	s := &sender{id: "a", to: "b", method: "ping"}
	eng.Register(s)
	eng.Schedule(0, "a", "go", nil)
	eng.Run(20)
	if len(b.got) != 1 || b.got[0] != 9 {
		t.Fatalf("message should arrive at t=9, got %v", b.got)
	}
}

func TestNetworkDuplicateRule(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{MinDelay: 1, Rules: []*sim.Rule{{
		Action: "duplicate", DupDelay: 3, Match: sim.Match{Method: "ping"},
	}}})
	b := &probe{id: "b"}
	eng.Register(b)
	s := &sender{id: "a", to: "b", method: "ping"}
	eng.Register(s)
	eng.Schedule(0, "a", "go", nil)
	eng.Run(20)
	if len(b.got) != 2 {
		t.Fatalf("expected 2 deliveries, got %d at %v", len(b.got), b.got)
	}
}

func TestNetworkNthMatchTargetsOneOccurrence(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{MinDelay: 1, Rules: []*sim.Rule{{
		Name: "only-second", Action: "drop", Match: sim.Match{Method: "ping", Nth: 2},
	}}})
	b := &probe{id: "b"}
	eng.Register(b)
	s := &sender{id: "a", to: "b", method: "ping"}
	eng.Register(s)
	eng.Schedule(0, "a", "go", nil)
	eng.Schedule(2, "a", "go", nil)
	eng.Schedule(4, "a", "go", nil)
	eng.Run(20)
	if len(b.got) != 2 {
		t.Fatalf("expected 2 deliveries (2nd dropped), got %d at %v", len(b.got), b.got)
	}
}

// TestPauseHoldsEventsAndFreesOnResume: while paused, neither messages nor
// timers reach the node; everything flushes at resume.
func TestPauseHoldsEventsAndFreesOnResume(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{MinDelay: 1})
	q := &probe{id: "q"}
	eng.Register(q)
	s := &sender{id: "x", to: "q", method: "ping"}
	eng.Register(s)
	eng.Pause("q", 10)
	eng.Schedule(3, "q", "frozen-timer", nil)
	eng.Schedule(2, "x", "go", nil) // send at 2, arrives 3, buffered
	eng.Run(20)
	if len(q.times) != 1 || q.times[0] != 10 {
		t.Fatalf("held timer should fire at resume t=10, got %v", q.times)
	}
	if len(q.got) != 1 || q.got[0] != 10 {
		t.Fatalf("held message should be delivered at resume t=10, got %v", q.got)
	}
}

type waker struct{ target string }

func (waker) NodeID() string { return "control" }
func (w *waker) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind == "timer" {
		env.Resume(w.target)
	}
}

func TestExplicitResumeFlushesEarly(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	p := &probe{id: "p"}
	eng.Register(p)
	eng.Register(&waker{target: "p"})
	eng.Pause("p", 100)
	eng.Schedule(3, "p", "t", nil)
	eng.Schedule(5, "control", "wake", nil)
	eng.Run(50)
	if len(p.times) != 1 || p.times[0] != 5 {
		t.Fatalf("explicit resume should flush timer at t=5, got %v", p.times)
	}
}

func TestCancelTimer(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	p := &probe{id: "p"}
	eng.Register(p)
	eng.Schedule(5, "p", "keep", nil)
	eng.Schedule(6, "p", "drop", nil)
	eng.CancelTimers("p", "drop")
	eng.Run(10)
	if len(p.times) != 1 || p.times[0] != 5 {
		t.Fatalf("only t=5 timer should fire, got %v", p.times)
	}
}
