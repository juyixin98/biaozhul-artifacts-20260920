// Package runner drives a scenario from a JSON description: it wires the lock
// service, one or more protected resources and client nodes into a simulation,
// schedules the scripted actions (acquire, submit, pause, network faults, ...)
// and evaluates the expected assertions against the trace.
package runner

import (
	"bytes"
	"encoding/json"
	"fmt"

	"fencinglease/internal/lock"
	"fencinglease/internal/node"
	"fencinglease/internal/resource"
	"fencinglease/internal/sim"
)

// Parse decodes a scenario JSON document. Unknown fields are rejected so that
// misspelled configuration keys fail loudly instead of being silently ignored.
func Parse(raw []byte, name string) (*Scenario, error) {
	var sc Scenario
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&sc); err != nil {
		return nil, fmt.Errorf("parse %s: %w", name, err)
	}
	if len(sc.Resources) == 0 {
		return nil, fmt.Errorf("scenario %s: at least one resource is required", name)
	}
	if sc.MaxTime <= 0 {
		return nil, fmt.Errorf("scenario %s: max_time must be > 0", name)
	}
	return &sc, nil
}

// Action is one scripted event. Exactly one command is meaningful per entry.
type Action struct {
	Time     int64     `json:"time"`
	Type     string    `json:"type"`           // acquire|submit|release|pause|resume|add_rule|compete
	Node     string    `json:"node,omitempty"` // target client (or paused node)
	Resource string    `json:"resource,omitempty"`
	TTL      int64     `json:"ttl,omitempty"`
	Value    string    `json:"value,omitempty"`
	Until    int64     `json:"until,omitempty"`  // pause
	Rule     *sim.Rule `json:"rule,omitempty"`   // add_rule
	Steps    int       `json:"steps,omitempty"`  // compete
	Every    int64     `json:"every,omitempty"`  // compete interval
	Prefix   string    `json:"prefix,omitempty"` // compete value prefix
}

// Expect is one assertion evaluated after the run. Min/Max are pointers so
// that 0 is an explicit bound ("require none") rather than an unset one.
type Expect struct {
	Kind     string   `json:"kind"` // record_count|records_exist|record_absent|accepted|rejected|last_water|granted_fences|accepted_order
	Node     string   `json:"node,omitempty"`
	Type     string   `json:"type,omitempty"` // trace record type
	Field    string   `json:"field,omitempty"`
	Equals   float64  `json:"equals,omitempty"`
	Min      *int     `json:"min,omitempty"`
	Max      *int     `json:"max,omitempty"`
	Resource string   `json:"resource,omitempty"`
	Order    []string `json:"order,omitempty"` // accepted node order
}

// ResourceCfg declares a protected resource.
type ResourceCfg struct {
	Name   string `json:"name"`
	NodeID string `json:"node_id"`
	Fenced bool   `json:"fenced"`
}

// Scenario is the full JSON input of "fls run".
type Scenario struct {
	Name      string        `json:"name"`
	Seed      int64         `json:"seed"`
	MaxTime   int64         `json:"max_time"`
	Network   sim.NetConfig `json:"network"`
	Resources []ResourceCfg `json:"resources"`
	Nodes     []string      `json:"nodes"`
	Actions   []Action      `json:"actions"`
	Expect    []Expect      `json:"expect"`
}

// Check is the outcome of one expectation.
type Check struct {
	Kind   string `json:"kind"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail"`
}

// Commit is one accepted resource write, summarized for the report.
type Commit struct {
	Time     int64  `json:"time"`
	Resource string `json:"resource"`
	Node     string `json:"node"`
	Fence    int64  `json:"fence"`
	Value    string `json:"value"`
}

// Result is the JSON output of a run.
type Result struct {
	Name    string       `json:"name"`
	Seed    int64        `json:"seed"`
	OK      bool         `json:"ok"`
	EndTime int64        `json:"end_time"`
	Checks  []Check      `json:"checks"`
	Commits []Commit     `json:"commits"`
	Traces  []sim.Record `json:"traces"`
}

// control is the scheduler node: its timers fire the scripted actions that
// are not node commands (pause/resume of arbitrary nodes, runtime rules).
type control struct {
	eng    *sim.Engine
	pauses map[string]int64 // target -> until, for scripted "resume"
	rules  []pendingRule
}

type pendingRule struct {
	at   int64
	rule *sim.Rule
}

func newController(eng *sim.Engine) *control {
	return &control{eng: eng, pauses: map[string]int64{}}
}

func (c *control) NodeID() string { return "control" }

func (c *control) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind != "timer" {
		return
	}
	target, _ := ev.Data["target"].(string)
	switch ev.Name {
	case "pause":
		until, _ := ev.Data["until"].(int64)
		c.pauses[target] = until
		c.eng.Pause(target, until)
	case "resume":
		c.eng.Resume(target)
		delete(c.pauses, target)
	case "add_rule":
		if r, ok := ev.Data["rule"].(*sim.Rule); ok {
			env.AddRule(r)
		}
	}
}

// Run executes a scenario and evaluates its expectations.
func Run(sc *Scenario) *Result {
	eng := sim.New(sc.Seed, sc.Network)
	lockSvc := lock.New(node.LockServer)
	eng.Register(lockSvc)

	resByName := map[string]*resource.Resource{}
	for _, rc := range sc.Resources {
		r := resource.New(rc.NodeID, rc.Fenced)
		eng.Register(r)
		resByName[rc.Name] = r
	}

	nodes := map[string]*node.Node{}
	resAddr := func(name string) string {
		if r, ok := resByName[name]; ok {
			return r.NodeID()
		}
		return name
	}
	for _, id := range sc.Nodes {
		nd := node.New(id, resAddr)
		eng.Register(nd)
		nodes[id] = nd
	}
	ctrl := newController(eng)
	eng.Register(ctrl)

	// Script the actions.
	for _, a := range sc.Actions {
		data := map[string]any{}
		switch a.Type {
		case "acquire", "submit", "release":
			data["resource"] = a.Resource
			data["ttl"] = a.TTL
			data["value"] = a.Value
			timerName := map[string]string{"acquire": node.CmdAcquire, "submit": node.CmdSubmit, "release": node.CmdRelease}[a.Type]
			eng.Schedule(a.Time, a.Node, timerName, data)
		case "pause":
			eng.Schedule(a.Time, ctrl.NodeID(), "pause", map[string]any{"target": a.Node, "until": a.Until})
		case "resume":
			eng.Schedule(a.Time, ctrl.NodeID(), "resume", map[string]any{"target": a.Node})
		case "add_rule":
			eng.Schedule(a.Time, ctrl.NodeID(), "add_rule", map[string]any{"rule": a.Rule})
		case "compete":
			data = map[string]any{
				"resource": a.Resource, "step": int64(0), "count": int64(a.Steps),
				"interval": a.Every, "prefix": a.Prefix,
			}
			eng.Schedule(a.Time, a.Node, node.CmdCompete, data)
		default:
			panic(fmt.Sprintf("runner: unknown action type %q", a.Type))
		}
	}

	eng.Run(sc.MaxTime)

	return evaluate(sc, eng, resByName)
}
