package sim

import "testing"

func TestSchedulerOrder(t *testing.T) {
	s := NewScheduler()
	var got []int
	s.At(10, "a", 10)
	s.At(5, "b", 5)
	s.At(10, "c", 10) // equal time -> insertion order after the first t=10 event
	s.At(1, "d", 1)
	s.Run(10, func(e *Event) { got = append(got, e.Data.(int)) })
	want := []int{1, 5, 10, 10}
	if len(got) != len(want) {
		t.Fatalf("got %v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order got %v want %v", got, want)
		}
	}
}

func TestSchedulerChainedSameTime(t *testing.T) {
	// An event at t=0 schedules a follow-up also at t=0; it must still fire
	// within the window (FIFO insertion order), which is how chained
	// broadcasts propagate at one virtual instant.
	s := NewScheduler()
	fired := 0
	var handle func(*Event)
	handle = func(e *Event) {
		fired++
		if fired == 1 {
			s.At(0, "x", nil)
		}
	}
	s.At(0, "x", nil)
	s.Run(0, handle)
	if fired != 2 {
		t.Fatalf("same-time chained event did not fire: %d", fired)
	}
}
