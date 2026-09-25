package scheduler

import (
	"strings"
	"testing"
)

func TestTimelineText(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "t", Base: 5, Arrival: 0, Program: Program{
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	out := TimelineText(r)
	for _, want := range []string{
		"mode=pip",
		"lockAcquired",
		"lockReleased",
		"---- tasks ----",
		"---- locks ----",
		"finish=1",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("timeline missing %q\n%s", want, out)
		}
	}
}
