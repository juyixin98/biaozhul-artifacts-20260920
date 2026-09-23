package engine

import "fmt"

// CrashHook is invoked at protocol milestones. The hook returns true if the
// crash targeted the node whose milestone fired (which aborts the remainder
// of that handler via CrashSentinel); a crash of some other node returns
// false and normal processing continues. When no crash rule matches, hooks
// return false.
type CrashHook func(id NodeID, milestone string) (crashedSelf bool)

// Base supplies the volatile parts shared by every simulated node:
// identity, up/down state, timer epochs and the crash hook slot.
type Base struct {
	NodeIDValue NodeID
	Down        bool
	Epoch       int
	Hook        CrashHook
}

func (b *Base) ID() NodeID { return b.NodeIDValue }

// IsDown reports whether the node is currently crashed/powered off.
func (b *Base) IsDown() bool { return b.Down }

// MarkDown clears volatile state. Nodes embed Base and override Crash() to
// wipe their own in-memory structures, then call this method.
func (b *Base) MarkDown() {
	b.Down = true
	b.Epoch++ // every timer scheduled before the crash is now stale
}

// MarkUp is called by restart logic.
func (b *Base) MarkUp() { b.Down = false }

// After schedules a one-shot timer. The token binds it to the current
// incarnation: a timer that fires after a crash is simply discarded.
func After(eng *Engine, b *Base, ticks int64, fire func(now int64)) {
	epoch := b.Epoch
	at := eng.Now() + ticks
	eng.ScheduleFunc(at, func(now int64) {
		if b.Down || b.Epoch != epoch {
			return
		}
		fire(now)
	})
}

// CrashAt runs the milestone hook and unwinds the current handler with
// CrashSentinel when that hook crashed this node. Durable writes above the
// hook are already on disk; sends / writes below it never execute.
func (b *Base) CrashAt(milestone string) {
	if b.Hook != nil && b.Hook(b.NodeIDValue, milestone) {
		panic(fmt.Errorf("milestone %s: %w", milestone, CrashSentinel))
	}
}
