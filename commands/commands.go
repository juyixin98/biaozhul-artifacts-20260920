// Package commands provides the built-in task kinds registered with
// the executor's HTTP server: small composable building blocks for
// recursion, fan-out, chains, sleeps, failures and panics.
package commands

import (
	"context"
	"fmt"
	"math/rand"
	"time"

	"worksteal/scheduler"
)

// Payload types for the built-in kinds. They are plain structs so the
// HTTP layer can decode request JSON directly into them.

// RecursePayload spawns two children of depth-1 and waits for both.
// Every level contributes 1 to the result, so a depth-D tree yields
// 2^(D+1)-1 executions. This is the starvation-deadlock canary: with
// workers=1 it can only complete if waiting workers help execute.
type RecursePayload struct {
	Depth int `json:"depth"`
}

// FanoutPayload spawns Count children of ChildKind and waits for all.
type FanoutPayload struct {
	Count     int    `json:"count"`
	ChildKind string `json:"child_kind"`
	Depth     int    `json:"depth,omitempty"` // forwarded to recurse children
	SleepMS   int    `json:"sleep_ms,omitempty"`
}

// ChainPayload runs Count sequential stages; each stage spawns the
// next and waits for it.
type ChainPayload struct {
	Count int `json:"count"`
}

// SleepPayload sleeps for Millis (clock-aware).
type SleepPayload struct {
	Millis int `json:"millis"`
}

// FailPayload returns an error carrying Message.
type FailPayload struct {
	Message string `json:"message"`
}

// PanicPayload panics with Message.
type PanicPayload struct {
	Message string `json:"message"`
}

// NoopPayload simply succeeds.
type NoopPayload struct{}

// Recursive returns a count of nodes in the subtree.
func recurseKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[RecursePayload](payload)
		if err != nil {
			return nil, err
		}
		if p.Depth <= 0 {
			return 1, nil
		}
		h1, err := rt.Spawn(ctx, "recurse", RecursePayload{Depth: p.Depth - 1})
		if err != nil {
			return nil, err
		}
		h2, err := rt.Spawn(ctx, "recurse", RecursePayload{Depth: p.Depth - 1})
		if err != nil {
			return nil, err
		}
		v1, err := rt.Wait(ctx, h1)
		if err != nil {
			return nil, err
		}
		v2, err := rt.Wait(ctx, h2)
		if err != nil {
			return nil, err
		}
		return 1 + toInt(v1) + toInt(v2), nil
	}
}

func fanoutKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[FanoutPayload](payload)
		if err != nil {
			return nil, err
		}
		child := p.ChildKind
		if child == "" {
			child = "noop"
		}
		hs := make([]scheduler.Handle, p.Count)
		for i := 0; i < p.Count; i++ {
			var childPayload any = struct{}{}
			switch child {
			case "recurse":
				childPayload = RecursePayload{Depth: p.Depth}
			case "sleep":
				childPayload = SleepPayload{Millis: p.SleepMS}
			case "fanout", "chain", "fail", "panic":
				return nil, fmt.Errorf("fanout child_kind %q is not a leaf kind", child)
			default:
				childPayload = NoopPayload{}
			}
			h, err := rt.Spawn(ctx, child, childPayload)
			if err != nil {
				return nil, err
			}
			hs[i] = h
		}
		sum := 0
		for _, h := range hs {
			v, err := rt.Wait(ctx, h)
			if err != nil {
				return nil, err
			}
			sum += toInt(v)
		}
		return sum, nil
	}
}

func chainKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[ChainPayload](payload)
		if err != nil {
			return nil, err
		}
		if p.Count <= 0 {
			return 0, nil
		}
		h, err := rt.Spawn(ctx, "chain", ChainPayload{Count: p.Count - 1})
		if err != nil {
			return nil, err
		}
		v, err := rt.Wait(ctx, h)
		if err != nil {
			return nil, err
		}
		return toInt(v) + 1, nil
	}
}

func sleepKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[SleepPayload](payload)
		if err != nil {
			return nil, err
		}
		if p.Millis < 0 {
			return nil, fmt.Errorf("millis must be >= 0")
		}
		if err := rt.Sleep(ctx, time.Duration(p.Millis)*time.Millisecond); err != nil {
			return nil, err
		}
		return "slept", nil
	}
}

func failKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[FailPayload](payload)
		if err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("%s", p.Message)
	}
}

func panicKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		p, err := decode[PanicPayload](payload)
		if err != nil {
			return nil, err
		}
		panic(p.Message)
	}
}

func noopKind() scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		return "ok", nil
	}
}

// jitterSleepKind sleeps a random short duration; used to create
// scheduling variety in stress scenarios.
func jitterSleepKind(maxMillis int) scheduler.TaskFunc {
	return func(ctx context.Context, rt scheduler.Runtime, payload any) (any, error) {
		d := time.Duration(rand.Intn(maxMillis+1)) * time.Millisecond
		if err := rt.Sleep(ctx, d); err != nil {
			return nil, err
		}
		return "jittered", nil
	}
}

// Register installs all built-in kinds on e.
func Register(e *scheduler.Executor) {
	e.RegisterKind("recurse", recurseKind())
	e.RegisterKind("fanout", fanoutKind())
	e.RegisterKind("chain", chainKind())
	e.RegisterKind("sleep", sleepKind())
	e.RegisterKind("fail", failKind())
	e.RegisterKind("panic", panicKind())
	e.RegisterKind("noop", noopKind())
	e.RegisterKind("jitter", jitterSleepKind(5))
}
