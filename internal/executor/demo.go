// Package executor contains a payload-driven demo Executor used by the HTTP
// server. The scheduler core stays independent of this payload schema.
package executor

import (
	"fmt"
	"strconv"
	"sync"
	"time"

	"dagscheduler/scheduler"
)

// Action names recognized in NodeSpec.Payload["action"].
const (
	ActionOK    = "ok"    // always succeed
	ActionFail  = "fail"  // always fail
	ActionFlaky = "flaky" // fail the first failTimes attempts, then succeed
	ActionSleep = "sleep" // sleep sleepMs, honoring cancellation
)

// Demo is an Executor driven by the node payload:
//
//	{"action": "ok"}
//	{"action": "fail", "message": "boom"}
//	{"action": "flaky", "failTimes": 2, "message": "transient"}
//	{"action": "sleep", "sleepMs": 500}
//
// Missing/unknown action means ok. It is safe for concurrent use; flaky
// attempt counters are keyed per (jobID, nodeName).
type Demo struct {
	mu       sync.Mutex
	failLeft map[string]int
}

// NewDemo builds a Demo executor.
func NewDemo() *Demo {
	return &Demo{failLeft: make(map[string]int)}
}

// Execute implements scheduler.Executor.
func (d *Demo) Execute(ec *scheduler.ExecContext, attempt int) error {
	action := payloadString(ec.Node.Payload, "action", ActionOK)
	switch action {
	case ActionOK:
		return nil
	case ActionFail:
		return fmt.Errorf("%s", payloadString(ec.Node.Payload, "message", "node failed"))
	case ActionFlaky:
		key := ec.JobID + "/" + ec.Node.Name
		d.mu.Lock()
		if _, seen := d.failLeft[key]; !seen {
			d.failLeft[key] = payloadInt(ec.Node.Payload, "failTimes", 1)
		}
		left := d.failLeft[key]
		if left > 0 {
			d.failLeft[key] = left - 1
			d.mu.Unlock()
			return fmt.Errorf("%s (flaky attempt %d)", payloadString(ec.Node.Payload, "message", "transient failure"), attempt)
		}
		d.mu.Unlock()
		return nil
	case ActionSleep:
		ms := payloadInt(ec.Node.Payload, "sleepMs", 100)
		return sleep(ec.Ctx, time.Duration(ms)*time.Millisecond)
	default:
		return fmt.Errorf("unknown action %q", action)
	}
}

func sleep(dc scheduler.DoneContext, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	if dc == nil {
		<-timer.C
		return nil
	}
	select {
	case <-timer.C:
		return nil
	case <-dc.Done():
		return fmt.Errorf("sleep interrupted: %w", dc.Err())
	}
}

func payloadString(p map[string]any, key, def string) string {
	if p == nil {
		return def
	}
	if v, ok := p[key]; ok {
		switch t := v.(type) {
		case string:
			return t
		case float64:
			return strconv.FormatFloat(t, 'f', -1, 64)
		case int:
			return strconv.Itoa(t)
		}
	}
	return def
}

func payloadInt(p map[string]any, key string, def int) int {
	if p == nil {
		return def
	}
	if v, ok := p[key]; ok {
		switch t := v.(type) {
		case float64:
			return int(t)
		case int:
			return t
		case string:
			if n, err := strconv.Atoi(t); err == nil {
				return n
			}
		}
	}
	return def
}
