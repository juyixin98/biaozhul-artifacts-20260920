// Package program defines the executable behaviour of a simulated task.
//
// A program is a sequence of zero or more Actions. Three action shapes exist:
//
//   - CPU work:        {"cpu": 3}
//   - acquire a lock:  {"acquire": "R1"}
//   - release a lock:  {"release": "R1"}
//
// The interface is intentionally small so the executor can be replaced: a
// custom Program implementation could, for example, drive a real goroutine
// behind a wall clock while still being scheduled by the same engine.
package program

import "fmt"

// Action is one step of a task program.
type Action struct {
	// CPU > 0 means "execute on the processor for CPU ticks".
	CPU int64 `json:"cpu,omitempty"`
	// Acquire, when non-empty, names a lock the task requests (blocking).
	Acquire string `json:"acquire,omitempty"`
	// Release, when non-empty, names a lock the task gives up. It must be
	// currently owned by the task.
	Release string `json:"release,omitempty"`
}

// Validate reports malformed actions (more than one field set, zero/negative
// CPU, empty lock names).
func (a Action) Validate() error {
	set := 0
	if a.CPU != 0 {
		set++
		if a.CPU < 0 {
			return fmt.Errorf("cpu duration must be non-negative, got %d", a.CPU)
		}
	}
	if a.Acquire != "" {
		set++
	}
	if a.Release != "" {
		set++
	}
	if set != 1 {
		return fmt.Errorf("action must set exactly one of cpu/acquire/release, got %+v", a)
	}
	return nil
}

// Program is the executor-facing contract. Next returns the next action to
// perform; ok == false means the program has terminated.
type Program interface {
	Next(pc int) (action Action, ok bool)
	Len() int
}

// ScriptProgram is the default, fixed action-list implementation.
type ScriptProgram struct {
	Actions []Action `json:"actions"`
}

// NewScript builds a ScriptProgram.
func NewScript(actions ...Action) *ScriptProgram { return &ScriptProgram{Actions: actions} }

func (p *ScriptProgram) Next(pc int) (Action, bool) {
	if pc < 0 || pc >= len(p.Actions) {
		return Action{}, false
	}
	return p.Actions[pc], true
}

func (p *ScriptProgram) Len() int { return len(p.Actions) }

// Validate checks every action in the script.
func (p *ScriptProgram) Validate() error {
	for i, a := range p.Actions {
		if err := a.Validate(); err != nil {
			return fmt.Errorf("action %d: %w", i, err)
		}
	}
	return nil
}
