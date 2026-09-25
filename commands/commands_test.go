package commands_test

import (
	"context"
	"testing"
	"time"

	"worksteal/commands"
	"worksteal/scheduler"
)

// TestSingleWorkerDeepTreeViaHTTPKinds drives the same task kinds the
// HTTP API exposes, with workers=1 and a deep binary tree. It is the
// direct acceptance scenario expressed against the registered kinds.
func TestSingleWorkerDeepTreeViaHTTPKinds(t *testing.T) {
	for _, workers := range []int{1, 4} {
		t.Run("", func(t *testing.T) {
			e, err := scheduler.New(scheduler.WithWorkers(workers))
			if err != nil {
				t.Fatal(err)
			}
			commands.Register(e)

			h, err := e.Submit("recurse", commands.RecursePayload{Depth: 10})
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			res, err := e.Result(ctx, h)
			if err != nil {
				t.Fatalf("Result: %v", err)
			}
			if res.State != scheduler.StateSucceeded {
				t.Fatalf("state=%s err=%s", res.State, res.Err)
			}
			want := (1 << 11) - 1 // 2047
			var got int
			switch n := res.Value.(type) {
			case int:
				got = n
			case float64:
				got = int(n)
			default:
				t.Fatalf("unexpected value type %T (%v)", res.Value, res.Value)
			}
			if got != want {
				t.Fatalf("nodes=%d want=%d (workers=%d)", got, want, workers)
			}
			if err := e.Shutdown(context.Background()); err != nil {
				t.Fatalf("Shutdown: %v", err)
			}
		})
	}
}

// TestPayloadDecodingThroughGenericMap exercises the decode path the
// HTTP layer uses (map[string]any after JSON decoding).
func TestPayloadDecodingThroughGenericMap(t *testing.T) {
	e, err := scheduler.New(scheduler.WithWorkers(1))
	if err != nil {
		t.Fatal(err)
	}
	commands.Register(e)
	h, err := e.Submit("sleep", map[string]any{"millis": 1})
	if err != nil {
		t.Fatal(err)
	}
	res, err := e.Result(context.Background(), h)
	if err != nil {
		t.Fatal(err)
	}
	if res.State != scheduler.StateSucceeded {
		t.Fatalf("state = %s err=%s", res.State, res.Err)
	}
	if err := e.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}
