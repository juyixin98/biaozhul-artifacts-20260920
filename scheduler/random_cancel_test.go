package scheduler

import (
	"context"
	"math/rand/v2"
	"sync"
	"sync/atomic"
	testing "testing"
	"time"
)

// TestRandomCancellation builds random spawn/wait trees with a fixed
// pool and concurrently cancels random tasks. It then drains
// everything and asserts the acceptance invariants:
//
//  1. every task function executes AT MOST once (per-id start count
//     from the structured event stream);
//  2. every created task reaches exactly ONE terminal state;
//  3. every root eventually completes and the executor drains to
//     zero outstanding tasks;
//  4. executor counters are internally consistent.
//
// It runs with workers=1 (the starvation-sensitive configuration) and
// with a larger pool where cross-worker steals actually happen.
func TestRandomCancellationSingleWorker(t *testing.T) {
	testRandomCancellation(t, 1, 40, 48)
}

func TestRandomCancellationMultiWorker(t *testing.T) {
	testRandomCancellation(t, 6, 80, 96)
}

func testRandomCancellation(t *testing.T, workers, roots, seed int) {
	t.Helper()
	buildRng := rand.New(rand.NewPCG(uint64(seed), uint64(seed+1)))
	cancelRng := rand.New(rand.NewPCG(uint64(seed+1000), uint64(seed+2000)))

	sink := newRecordingSink()
	e := newTestExecutor(t, workers, sink)

	// rngMu guards buildRng: tasks run on several worker goroutines.
	var rngMu sync.Mutex
	intn := func(n int) int {
		rngMu.Lock()
		v := buildRng.IntN(n)
		rngMu.Unlock()
		return v
	}

	var invocations atomic.Int64
	var build Func
	build = func(ctx context.Context, rt Runtime) (any, error) {
		d, _ := ctx.Value(depthCtxKey{}).(int)
		invocations.Add(1)

		// Cooperatively sleep sometimes to widen cancellation windows.
		if intn(3) == 0 {
			if err := rt.Sleep(ctx, time.Duration(intn(3))*time.Millisecond); err != nil {
				return nil, err
			}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}

		if d <= 0 || intn(10) < 3 {
			return 1, nil
		}
		n := intn(4) // 0..3 children
		hs := make([]Handle, 0, n)
		for i := 0; i < n; i++ {
			h, err := rt.SpawnFunc(context.WithValue(ctx, depthCtxKey{}, d-1), "node", build)
			if err != nil {
				return nil, err
			}
			hs = append(hs, h)
		}
		sum := 1
		for _, h := range hs {
			v, err := rt.Wait(ctx, h)
			if err != nil {
				if ctx.Err() != nil {
					return nil, ctx.Err()
				}
				continue
			}
			sum += toIntI(v)
		}
		return sum, nil
	}

	rootHandles := make([]Handle, 0, roots)
	for i := 0; i < roots; i++ {
		rngMu.Lock()
		d := buildRng.IntN(5) + 2
		rngMu.Unlock()
		h, err := e.SubmitFunc("node", func(ctx context.Context, rt Runtime) (any, error) {
			return build(context.WithValue(ctx, depthCtxKey{}, d), rt)
		})
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		rootHandles = append(rootHandles, h)
	}

	// Concurrently cancel random live tasks sampled from the event
	// stream, plus the occasional root.
	stop := make(chan struct{})
	var cancelCalls, cancelOK atomic.Int64
	go func() {
		for {
			select {
			case <-stop:
				return
			default:
			}
			live := liveTaskIDs(sink.slice())
			if len(live) > 0 {
				id := live[cancelRng.IntN(len(live))]
				if h := e.HandleByID(id); h != nil {
					cancelCalls.Add(1)
					if ok, err := e.Cancel(h); err == nil && ok {
						cancelOK.Add(1)
					}
				}
			}
			if len(rootHandles) > 0 && cancelRng.IntN(8) == 0 {
				_, _ = e.Cancel(rootHandles[cancelRng.IntN(len(rootHandles))])
			}
			time.Sleep(time.Duration(cancelRng.IntN(2)) * time.Millisecond)
		}
	}()

	for _, h := range rootHandles {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		_, _ = e.Result(ctx, h)
		cancel()
	}
	close(stop)
	if err := e.WaitIdle(context.Background()); err != nil {
		t.Fatalf("WaitIdle: %v", err)
	}

	// Invariants 1 & 2 from the structured event stream.
	startedByID := map[string]int{}
	terminalByID := map[string]State{}
	var maxSeq int64
	orderOK := true
	for _, ev := range sink.slice() {
		if ev.Seq <= maxSeq {
			orderOK = false
		}
		maxSeq = ev.Seq
		switch ev.Type {
		case EventStarted:
			startedByID[ev.TaskID]++
		case EventCompleted:
			if prev, dup := terminalByID[ev.TaskID]; dup {
				t.Fatalf("task %s reached two terminal states: %s and %s",
					ev.TaskID, prev, ev.State)
			}
			terminalByID[ev.TaskID] = ev.State
		}
	}
	if !orderOK {
		t.Fatal("event sequence numbers are not strictly increasing")
	}
	for id, n := range startedByID {
		if n > 1 {
			t.Fatalf("task %s started %d times (at-most-once violated)", id, n)
		}
	}

	created := map[string]bool{}
	for _, ev := range sink.slice() {
		if ev.Type == EventSubmitted || ev.Type == EventSpawned {
			created[ev.TaskID] = true
		}
	}
	if len(terminalByID) != len(created) {
		t.Fatalf("terminal=%d created=%d", len(terminalByID), len(created))
	}
	for id := range created {
		if _, ok := terminalByID[id]; !ok {
			t.Fatalf("task %s never reached a terminal state", id)
		}
	}
	if got := int(invocations.Load()); got != len(startedByID) {
		t.Fatalf("function invocations=%d but distinct started tasks=%d",
			got, len(startedByID))
	}

	stats := e.Stats()
	if stats.Outstanding != 0 || stats.Running != 0 ||
		stats.QueuedLocal != 0 || stats.QueuedGlobal != 0 {
		t.Fatalf("executor not drained: %+v", stats)
	}
	terminalTotal := stats.Succeeded + stats.Failed + stats.Panicked + stats.Canceled
	if terminalTotal != len(created) {
		t.Fatalf("stats terminal total %d != created %d (%+v)",
			terminalTotal, len(created), stats)
	}
	if stats.Started > terminalTotal {
		t.Fatalf("started %d > terminal %d", stats.Started, terminalTotal)
	}
	t.Logf("workers=%d created=%d started=%d succeeded=%d failed=%d panicked=%d canceled=%d cancelCalls=%d cancelOK=%d",
		workers, len(created), stats.Started, stats.Succeeded, stats.Failed,
		stats.Panicked, stats.Canceled, cancelCalls.Load(), cancelOK.Load())
}

type depthCtxKey struct{}

// liveTaskIDs returns ids seen as started (or submitted/spawned) but
// not yet completed in the given event slice.
func liveTaskIDs(evs []Event) []string {
	started := map[string]bool{}
	done := map[string]bool{}
	for _, e := range evs {
		switch e.Type {
		case EventStarted, EventSubmitted, EventSpawned:
			started[e.TaskID] = true
		case EventCompleted:
			done[e.TaskID] = true
		}
	}
	out := make([]string, 0)
	for id := range started {
		if !done[id] {
			out = append(out, id)
		}
	}
	return out
}
