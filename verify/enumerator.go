package verify

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"time"
)

// EnumReport summarizes an enumeration campaign.
type EnumReport struct {
	Variant      string        `json:"variant"`
	Nodes        int           `json:"nodes"`
	Depth        int           `json:"depth"`
	ExhaustiveN  int           `json:"exhaustiveRan"`
	FuzzN        int           `json:"fuzzRan"`
	Counterex    []Result      `json:"counterexamples"`
	Elapsed      time.Duration `json:"elapsed"`
	Truncated    bool          `json:"truncated"`
	MaxScenarios int           `json:"maxScenarios"`
}

// macro is a reusable pre-expanded action bundle. Commands use the macro
// position so each proposal along a trace carries a distinct, path-dependent
// value (SET k v0, v1, ...), which makes committed-prefix conflicts visible.
type macro struct {
	name  string
	build func(pos int) []Action
}

func adv(ms int64) Action { return Action{Kind: KindAdvance, AdvanceMs: ms} }

func macroAlphabet() []macro {
	macros := []macro{
		{"settle", func(int) []Action { return []Action{adv(settleMs)} }},
		{"replicate", func(int) []Action { return []Action{adv(replMs)} }},
		{"propose", func(pos int) []Action {
			return []Action{
				{Kind: KindProposeAll, Command: fmt.Sprintf("SET k v%d", pos)},
				adv(replMs),
			}
		}},
		{"heal", func(int) []Action { return []Action{{Kind: KindHeal}} }},
	}
	for _, id := range []int{1, 2, 3} {
		id := id
		macros = append(macros,
			macro{
				name: fmt.Sprintf("isolate%d", id),
				build: func(int) []Action {
					return []Action{
						{Kind: KindPause, Node: id},
						{Kind: KindPartition, Peers: []int{id}},
					}
				},
			},
			macro{
				name: fmt.Sprintf("partition%d", id),
				build: func(int) []Action {
					return []Action{{Kind: KindPartition, Peers: []int{id}}}
				},
			},
			macro{
				name: fmt.Sprintf("healresume%d", id),
				build: func(int) []Action {
					return []Action{{Kind: KindHeal}, {Kind: KindResume, Node: id}}
				},
			},
			macro{
				// Freeze a node's outbound stream, letting stale messages pile
				// up for a later release.
				name: fmt.Sprintf("freezeOut%d", id),
				build: func(int) []Action {
					return []Action{{Kind: KindPauseFrom, Node: id}}
				},
			},
			macro{
				name: fmt.Sprintf("releaseOut%d", id),
				build: func(int) []Action {
					return []Action{{Kind: KindHeal}, {Kind: KindResumeFrom, Node: id}, adv(settleMs)}
				},
			},
			macro{
				name: fmt.Sprintf("restart%d", id),
				build: func(int) []Action {
					return []Action{{Kind: KindRestart, Node: id}, adv(settleMs / 2)}
				},
			},
		)
	}
	return macros
}

// prefix actions before the enumerated suffix: elect a leader and replicate
// one committed entry.
func enumPrefix() []Action {
	return []Action{
		adv(settleMs),
		{Kind: KindProposeAll, Command: "SET k v0"},
		adv(replMs),
	}
}

type envState struct {
	partitioned map[int]bool
	paused      map[int]bool
	frozen      map[int]bool
}

// Enumerate runs bounded exhaustive short traces (every macro sequence up to
// depth, pruning adjacent no-ops) plus seeded random fuzz traces. It stops
// expanding after the first maxCE counterexamples are found but always runs
// the full exhaustive set when maxCE allows.
func Enumerate(variant string, depth, fuzzN int, maxCE int) EnumReport {
	start := time.Now()
	rep := EnumReport{Variant: variant, Nodes: 3, Depth: depth, MaxScenarios: 20000, Counterex: []Result{}}
	alphabet := macroAlphabet()

	prefix := enumPrefix()
	st0 := envState{partitioned: map[int]bool{}, paused: map[int]bool{}, frozen: map[int]bool{}}

	seenCE := map[string]bool{}
	addCE := func(r Result) bool {
		key := r.Violations[0].Invariant + "|" + r.Scenario.String()
		if seenCE[key] {
			return false
		}
		seenCE[key] = true
		// Re-run with a trace for the first few counterexamples for replay.
		traced := Run(r.Scenario, Options{Trace: true, CaptureViews: true, EpilogueMs: 60})
		rep.Counterex = append(rep.Counterex, traced)
		return len(rep.Counterex) >= maxCE
	}

	var dfs func(cur []Action, st envState, d int) bool
	dfs = func(cur []Action, st envState, d int) bool {
		if d == 0 {
			if len(cur) == 0 {
				return false
			}
			if rep.ExhaustiveN >= rep.MaxScenarios {
				rep.Truncated = true
				return true
			}
			sc := Scenario{
				Name:    "enum-" + hashKey(cur),
				Nodes:   3,
				Variant: variant,
				Seed:    777,
				Actions: append(append([]Action(nil), prefix...), cur...),
			}
			rep.ExhaustiveN++
			r := Run(sc, Options{EpilogueMs: 60})
			if !r.OK && addCE(r) {
				return true
			}
			return false
		}
		for i, m := range alphabet {
			next, nst := applyMacro(m, i, cur, st)
			if next == nil {
				continue
			}
			if dfs(next, nst, d-1) {
				return true
			}
		}
		return false
	}
	dfs(nil, st0, depth)

	// Seeded random fuzz: longer traces mixing the same fault macros.
	master := rand.New(rand.NewSource(20260924))
	for f := 0; f < fuzzN && len(rep.Counterex) < maxCE; f++ {
		seq := make([]Action, 0, 8)
		st := envState{partitioned: map[int]bool{}, paused: map[int]bool{}, frozen: map[int]bool{}}
		pos := 0
		steps := 5 + master.Intn(3)
		for k := 0; k < steps; k++ {
			m := alphabet[master.Intn(len(alphabet))]
			var expanded []Action
			if m.name == "propose" {
				expanded = m.build(pos)
				pos++
			} else {
				expanded = m.build(pos)
			}
			// validate prune
			ok := true
			for _, a := range expanded {
				switch a.Kind {
				case KindPartition:
					if st.partitioned[a.Peers[0]] {
						ok = false
					}
				case KindHeal:
					if len(st.partitioned) == 0 {
						ok = false
					}
				}
			}
			if !ok {
				continue
			}
			seq, st = applyState(seq, st, expanded)
		}
		sc := Scenario{
			Name:    fmt.Sprintf("fuzz-%d", f),
			Nodes:   3,
			Variant: variant,
			Seed:    master.Int63(),
			Actions: append(append([]Action(nil), prefix...), seq...),
		}
		rep.FuzzN++
		r := Run(sc, Options{EpilogueMs: 60})
		if !r.OK && addCE(r) {
			break
		}
	}

	rep.Elapsed = time.Since(start)
	return rep
}

// applyMacro expands a macro and returns nil if it would be an adjacent no-op.
func applyMacro(m macro, idx int, cur []Action, st envState) ([]Action, envState) {
	pos := 0
	for _, a := range cur {
		if a.Kind == KindProposeAll {
			pos++
		}
	}
	expanded := m.build(pos)

	// stateful prune checks
	for _, a := range expanded {
		switch a.Kind {
		case KindPartition:
			if st.partitioned[a.Peers[0]] {
				return nil, st
			}
		case KindHeal:
			if len(st.partitioned) == 0 {
				return nil, st
			}
		case KindPause:
			if st.paused[a.Node] {
				return nil, st
			}
		case KindResume:
			if !st.paused[a.Node] {
				return nil, st
			}
		case KindPauseFrom:
			if st.frozen[a.Node] {
				return nil, st
			}
		case KindResumeFrom:
			if !st.frozen[a.Node] {
				return nil, st
			}
		}
	}
	// avoid two consecutive pure time advances
	if len(expanded) == 1 && expanded[0].Kind == KindAdvance &&
		len(cur) > 0 && cur[len(cur)-1].Kind == KindAdvance {
		return nil, st
	}
	return applyState(cur, st, expanded)
}

func applyState(cur []Action, st envState, expanded []Action) ([]Action, envState) {
	ns := envState{
		partitioned: map[int]bool{},
		paused:      map[int]bool{},
		frozen:      map[int]bool{},
	}
	for k, v := range st.partitioned {
		ns.partitioned[k] = v
	}
	for k, v := range st.paused {
		ns.paused[k] = v
	}
	for k, v := range st.frozen {
		ns.frozen[k] = v
	}
	for _, a := range expanded {
		switch a.Kind {
		case KindPartition:
			for _, id := range a.Peers {
				ns.partitioned[id] = true
			}
		case KindHeal:
			ns.partitioned = map[int]bool{}
		case KindPause:
			ns.paused[a.Node] = true
		case KindResume:
			delete(ns.paused, a.Node)
		case KindPauseFrom:
			ns.frozen[a.Node] = true
		case KindResumeFrom:
			delete(ns.frozen, a.Node)
		}
	}
	return append(append([]Action(nil), cur...), expanded...), ns
}

func hashKey(as []Action) string {
	h := fnv.New32a()
	for _, a := range as {
		fmt.Fprintf(h, "%s/%d/%v/%s/%d|", a.Kind, a.Node, a.Peers, a.Command, a.AdvanceMs)
	}
	return fmt.Sprintf("%08x", h.Sum32())
}
