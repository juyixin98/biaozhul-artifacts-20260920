// Command drfsim replays the hand-calculated acceptance scenario from
// README.md against the FakeClock and prints every state-change event.
// It exists so the documented allocation sequence can be reproduced
// without any wall-clock waiting:
//
//	go run ./cmd/drfsim
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"fairdrf/scheduler"
)

type sub struct {
	id, tenant string
	cpu, mem   int64
	d          time.Duration
	at         time.Duration // submission offset from t=0
}

func main() {
	clock := scheduler.NewFakeClock(time.Unix(0, 0).UTC())
	store := scheduler.NewMemoryStore()
	sched, err := scheduler.New(
		scheduler.Resources{CPU: 10, Mem: 10},
		scheduler.WithClock(clock),
		scheduler.WithExecutor(scheduler.NewTimedExecutor(clock)),
		scheduler.WithEventStore(store),
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, id := range []string{"A", "B", "C"} {
		if err := sched.AddTenant(id, 1); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}

	tasks := []sub{
		{"T1", "A", 6, 1, 2 * time.Second, 0},
		{"T2", "A", 6, 6, 3 * time.Second, 0},
		{"T3", "B", 4, 9, 5 * time.Second, 0},
		{"T4", "C", 9, 9, 5 * time.Second, 0},
		{"T5", "B", 1, 1, 1 * time.Second, 0},
		{"T6", "B", 2, 2, 3 * time.Second, 0},
		{"T7", "C", 1, 1, 3 * time.Second, 0},
		{"T8", "B", 1, 1, 1 * time.Second, 1 * time.Second},
	}

	enc := json.NewEncoder(os.Stdout)
	// Submit tasks at their offsets, then advance one fake-clock second
	// at a time so each completion and the follow-up admission both get
	// their correct logical timestamps (a bulk jump would timestamp the
	// cascade at the final instant).
	tick := time.Second
	horizon := 16 * time.Second
	ti := 0
	for now := time.Duration(0); now <= horizon; now += tick {
		for ti < len(tasks) && tasks[ti].at == now {
			tk := tasks[ti]
			ti++
			if err := sched.Submit(scheduler.TaskSpec{
				ID: tk.id, TenantID: tk.tenant,
				Request:  scheduler.Resources{CPU: tk.cpu, Mem: tk.mem},
				Duration: scheduler.Duration{Duration: tk.d},
			}); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
		}
		if now < horizon {
			clock.Advance(tick)
		}
	}

	for _, ev := range store.All() {
		_ = enc.Encode(ev)
	}
}
