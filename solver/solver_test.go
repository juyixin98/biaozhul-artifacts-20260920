package solver_test

import (
	"fmt"
	"math"
	"math/rand"
	"testing"

	"robotdispatch/model"
	"robotdispatch/solver"
)

// ---------------------------------------------------------------------------
// Independent brute-force reference implementation.
//
// This code deliberately does not reuse anything from package solver: it
// re-simulates movement/charging/time windows from scratch, enumerates
// every feasible per-robot route (ordering × charging detours) and then
// combines per-robot routes into disjoint task covers. The minimum
// makespan found here is the true optimum and is compared against the
// solver output.
// ---------------------------------------------------------------------------

type refState struct {
	x, y   int64
	t, bat int64
}

type refStep struct {
	charge bool
	chIdx  int
	task   int
}

// refRoute computes, for one robot, the earliest achievable finish time
// for every reachable subset of tasks. Position after a subset is fixed by
// the last served task, so states are (time, battery) pairs kept as a
// Pareto frontier: a later-arriving state with no more battery is
// dominated and discarded.
func refRoute(rob model.Robot, tasks []model.Task, chargers []model.Charger, epd int64) map[uint16]int64 {
	type state struct {
		last   int // -1 at start
		t, bat int64
	}
	type key struct {
		mask uint16
		last int
	}
	frontier := map[key][]state{}
	queued := map[key]bool{}
	startKey := key{0, -1}
	frontier[startKey] = []state{{last: -1, t: 0, bat: rob.Battery}}
	queue := []key{startKey}
	queued[startKey] = true

	best := map[uint16]int64{0: 0}

	try := func(st state, x, y int64, k int, chIdx int) (state, bool) {
		t, bat := st.t, st.bat
		if chIdx >= 0 {
			c := chargers[chIdx]
			d1 := abs(x-c.Loc.X) + abs(y-c.Loc.Y)
			c1 := epd * d1
			if bat < c1 {
				return state{}, false
			}
			t += d1
			bat -= c1
			need := rob.BatteryCapacity - bat
			var ct int64
			if need > 0 {
				ct = (need + rob.ChargeRate - 1) / rob.ChargeRate
			}
			t += ct
			bat = rob.BatteryCapacity
			x, y = c.Loc.X, c.Loc.Y
		}
		tk := tasks[k]
		d := abs(x-tk.Loc.X) + abs(y-tk.Loc.Y)
		c := epd * d
		if bat < c {
			return state{}, false
		}
		arrive := t + d
		bat -= c
		start := arrive
		if tk.Ready > start {
			start = tk.Ready
		}
		if start > tk.Due {
			return state{}, false
		}
		return state{last: k, t: start + tk.Service, bat: bat}, true
	}

	pos := func(last int) (int64, int64) {
		if last < 0 {
			return rob.Start.X, rob.Start.Y
		}
		return tasks[last].Loc.X, tasks[last].Loc.Y
	}

	// addState inserts ns into the frontier of its key, pruning dominated
	// states; returns whether it survived.
	addState := func(mask uint16, ns state) bool {
		k := key{mask, ns.last}
		for _, ex := range frontier[k] {
			if ex.t <= ns.t && ex.bat >= ns.bat {
				return false
			}
		}
		kept := frontier[k][:0]
		for _, ex := range frontier[k] {
			if !(ns.t <= ex.t && ns.bat >= ex.bat) {
				kept = append(kept, ex)
			}
		}
		kept = append(kept, ns)
		if frontier[k] == nil {
			queue = append(queue, k)
		}
		frontier[k] = kept
		if old, ok := best[mask]; !ok || ns.t < old {
			best[mask] = ns.t
		}
		return true
	}

	for qi := 0; qi < len(queue); qi++ {
		k := queue[qi]
		x, y := pos(k.last)
		for _, st := range frontier[k] {
			for ti := range tasks {
				if k.mask&(1<<uint(ti)) != 0 {
					continue
				}
				if rob.PayloadCapacity < tasks[ti].Payload {
					continue
				}
				nm := k.mask | 1<<uint(ti)
				if nn, ok := try(st, x, y, ti, -1); ok {
					addState(nm, nn)
				}
				for ci := range chargers {
					if nn, ok := try(st, x, y, ti, ci); ok {
						addState(nm, nn)
					}
				}
			}
		}
	}
	return best
}

// refOptimal combines per-robot route tables into the optimal makespan.
// Returns (false, 0) when the tasks cannot be covered.
func refOptimal(req *model.Request, epd int64) (bool, int64) {
	tables := make([]map[uint16]int64, len(req.Robots))
	for i, rb := range req.Robots {
		tables[i] = refRoute(rb, req.Tasks, req.Chargers, epd)
	}
	full := uint16(1)<<uint(len(req.Tasks)) - 1

	var opt int64 = math.MaxInt64
	var rec func(robot int, used uint16, span int64)
	rec = func(robot int, used uint16, span int64) {
		if span >= opt {
			return
		}
		if robot == len(req.Robots) {
			if used == full && span < opt {
				opt = span
			}
			return
		}
		for m, fin := range tables[robot] {
			if m&used != 0 {
				continue
			}
			ns := span
			if fin > ns {
				ns = fin
			}
			rec(robot+1, used|m, ns)
		}
	}
	rec(0, 0, 0)
	return opt != math.MaxInt64, opt
}

func abs(a int64) int64 {
	if a < 0 {
		return -a
	}
	return a
}

// ---------------------------------------------------------------------------
// Plan property verification: every leg battery non-negative, time windows
// respected, every task served exactly once, charging math consistent.
// ---------------------------------------------------------------------------

func verifyPlan(t *testing.T, req *model.Request, resp *model.Response, wantMakespan int64) {
	t.Helper()
	if !resp.Feasible {
		t.Fatalf("expected feasible plan, got reason: %s", resp.Reason)
	}
	if resp.Makespan != wantMakespan {
		t.Fatalf("makespan: got %d want %d", resp.Makespan, wantMakespan)
	}
	if len(resp.Plans) != len(req.Robots) {
		t.Fatalf("plans for %d robots, want %d", len(resp.Plans), len(req.Robots))
	}

	charger := map[string]model.Charger{}
	for _, c := range req.Chargers {
		charger[c.ID] = c
	}
	robots := map[string]model.Robot{}
	for _, rb := range req.Robots {
		robots[rb.ID] = rb
	}
	task := map[string]model.Task{}
	for _, tk := range req.Tasks {
		task[tk.ID] = tk
	}

	seenTasks := map[string]int{}
	var globalFinish int64
	for _, p := range resp.Plans {
		rb, ok := robots[p.RobotID]
		if !ok {
			t.Fatalf("plan for unknown robot %q", p.RobotID)
		}
		bat := rb.Battery
		pos := rb.Start
		var lastEnd int64
		for i, st := range p.Steps {
			if st.From != pos {
				t.Fatalf("robot %s step %d: from %v not at previous location %v", p.RobotID, i, st.From, pos)
			}
			d := model.Dist(st.From, st.To)
			if st.MoveDist != d || st.MoveTime != d {
				t.Fatalf("robot %s step %d: move dist/time mismatch", p.RobotID, i)
			}
			if st.MoveCost != req.EnergyPerDistOr1()*d {
				t.Fatalf("robot %s step %d: move cost mismatch", p.RobotID, i)
			}
			if st.ArriveTime != lastEnd+st.MoveTime {
				t.Fatalf("robot %s step %d: arrival time %d, want %d", p.RobotID, i, st.ArriveTime, lastEnd+st.MoveTime)
			}
			// energy for the leg is paid on arrival and must never go negative
			if bat < st.MoveCost {
				t.Fatalf("robot %s step %d: battery %d < leg cost %d (would go negative)", p.RobotID, i, bat, st.MoveCost)
			}
			bat -= st.MoveCost
			if st.BatteryBefore != bat || bat < 0 {
				t.Fatalf("robot %s step %d: batteryBefore %d, want %d", p.RobotID, i, st.BatteryBefore, bat)
			}

			switch st.Type {
			case "task":
				tk := task[st.TaskID]
				if rb.PayloadCapacity < tk.Payload {
					t.Fatalf("robot %s cannot carry task %s", p.RobotID, st.TaskID)
				}
				if st.StartTime < tk.Ready || st.StartTime > tk.Due {
					t.Fatalf("task %s served at %d outside window [%d,%d]", st.TaskID, st.StartTime, tk.Ready, tk.Due)
				}
				if st.StartTime < st.ArriveTime {
					t.Fatalf("task %s starts before arrival", st.TaskID)
				}
				if st.EndTime != st.StartTime+tk.Service {
					t.Fatalf("task %s end mismatch", st.TaskID)
				}
				if st.BatteryAfter != bat {
					t.Fatalf("task %s batteryAfter mismatch", st.TaskID)
				}
				seenTasks[st.TaskID]++
			case "charge":
				c := charger[st.ChargerID]
				if c.Loc != st.To {
					t.Fatalf("charger %s location mismatch", st.ChargerID)
				}
				wantCT := int64(0)
				if need := rb.BatteryCapacity - bat; need > 0 {
					wantCT = (need + rb.ChargeRate - 1) / rb.ChargeRate
				}
				if st.ChargeTime != wantCT {
					t.Fatalf("charger %s charge time %d want %d", st.ChargerID, st.ChargeTime, wantCT)
				}
				if st.EndTime != st.ArriveTime+st.ChargeTime {
					t.Fatalf("charger %s end time mismatch", st.ChargerID)
				}
				if st.BatteryAfter != rb.BatteryCapacity {
					t.Fatalf("charger %s leaves battery %d, want full %d", st.ChargerID, st.BatteryAfter, rb.BatteryCapacity)
				}
				bat = rb.BatteryCapacity
			default:
				t.Fatalf("unknown step type %q", st.Type)
			}
			pos = st.To
			lastEnd = st.EndTime
		}
		if p.FinishAt != lastEnd {
			t.Fatalf("robot %s FinishAt %d, want %d", p.RobotID, p.FinishAt, lastEnd)
		}
		if lastEnd > globalFinish {
			globalFinish = lastEnd
		}
	}
	if globalFinish != resp.Makespan {
		t.Fatalf("max finish over robots %d != reported makespan %d", globalFinish, resp.Makespan)
	}
	if len(seenTasks) != len(req.Tasks) {
		t.Fatalf("served %d distinct tasks, want %d", len(seenTasks), len(req.Tasks))
	}
	for id, n := range seenTasks {
		if n != 1 {
			t.Fatalf("task %s served %d times, want exactly once", id, n)
		}
	}
}

func solveReq(req *model.Request) *model.Response {
	epd, err := req.Validate()
	if err != nil {
		panic(err)
	}
	return solver.Solve(req, epd)
}

// ---------------------------------------------------------------------------
// Targeted scenarios
// ---------------------------------------------------------------------------

func p(x, y int64) model.Point { return model.Point{X: x, Y: y} }

// Task far away: the robot cannot reach it without charging first.
func TestMustChargeBeforeFirstTask(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 6, BatteryCapacity: 20, ChargeRate: 2, PayloadCapacity: 10},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(10, 0), Payload: 1, Ready: 0, Due: 100, Service: 1},
		},
		Chargers: []model.Charger{{ID: "c1", Loc: p(5, 0)}},
	}
	resp := solveReq(req)
	ok, opt := refOptimal(req, 1)
	if !ok {
		t.Fatal("reference says infeasible")
	}
	verifyPlan(t, req, resp, opt)
	if len(resp.Plans[0].Steps) != 2 || resp.Plans[0].Steps[0].Type != "charge" {
		t.Fatalf("expected a charge detour then the task, got %+v", resp.Plans[0].Steps)
	}
}

// Same instance without a charger: must be infeasible (energy negative).
func TestMustChargeNoChargerInfeasible(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 6, BatteryCapacity: 20, ChargeRate: 2, PayloadCapacity: 10},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(10, 0), Payload: 1, Ready: 0, Due: 100, Service: 1},
		},
	}
	resp := solveReq(req)
	if resp.Feasible {
		t.Fatalf("expected infeasible, got plan: %+v", resp.Plans)
	}
	ok, _ := refOptimal(req, 1)
	if ok {
		t.Fatal("reference unexpectedly found a feasible plan")
	}
}

// Tight windows: two robots each have a window barely wide enough; any
// waiting or detour makes the window impossible.
func TestTightTimeWindows(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 100, BatteryCapacity: 100, ChargeRate: 5, PayloadCapacity: 10},
			{ID: "r2", Start: p(0, 0), Battery: 100, BatteryCapacity: 100, ChargeRate: 5, PayloadCapacity: 10},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(4, 0), Payload: 1, Ready: 4, Due: 4, Service: 2},
			{ID: "j2", Loc: p(5, 0), Payload: 1, Ready: 5, Due: 5, Service: 3},
		},
	}
	resp := solveReq(req)
	ok, opt := refOptimal(req, 1)
	if !ok {
		t.Fatal("reference says infeasible")
	}
	verifyPlan(t, req, resp, opt) // optimum makespan = 8
}

// A window so tight that even the earliest possible arrival is too late.
func TestTightWindowInfeasible(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 100, BatteryCapacity: 100, ChargeRate: 5, PayloadCapacity: 10},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(10, 0), Payload: 1, Ready: 0, Due: 5, Service: 1},
		},
	}
	resp := solveReq(req)
	if resp.Feasible {
		t.Fatal("expected infeasible due to due window")
	}
	ok, _ := refOptimal(req, 1)
	if ok {
		t.Fatal("reference unexpectedly feasible")
	}
}

// Payload capacity makes the instance impossible.
func TestPayloadInfeasible(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 100, BatteryCapacity: 100, ChargeRate: 5, PayloadCapacity: 2},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(1, 0), Payload: 5, Ready: 0, Due: 100, Service: 1},
		},
	}
	resp := solveReq(req)
	if resp.Feasible {
		t.Fatal("expected infeasible due to payload")
	}
}

// A mid-route recharge: the battery suffices for the first task but not
// both; the optimal plan recharges between the two tasks.
func TestChargeBetweenTasks(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 9, BatteryCapacity: 30, ChargeRate: 10, PayloadCapacity: 10},
		},
		Tasks: []model.Task{
			{ID: "j1", Loc: p(5, 0), Payload: 1, Ready: 0, Due: 100, Service: 0},
			{ID: "j2", Loc: p(10, 0), Payload: 1, Ready: 0, Due: 100, Service: 1},
		},
		Chargers: []model.Charger{{ID: "c1", Loc: p(5, 0)}},
	}
	resp := solveReq(req)
	ok, opt := refOptimal(req, 1)
	if !ok {
		t.Fatal("reference says infeasible")
	}
	verifyPlan(t, req, resp, opt)

	// locate the charge step and prove it sits between the two task services
	order := []string{}
	for _, st := range resp.Plans[0].Steps {
		if st.Type == "charge" {
			order = append(order, "charge:"+st.ChargerID)
		} else {
			order = append(order, st.TaskID)
		}
	}
	want := []string{"j1", "charge:c1", "j2"}
	if fmt.Sprint(order) != fmt.Sprint(want) {
		t.Fatalf("step order = %v, want %v", order, want)
	}
}

func TestEmptyTasks(t *testing.T) {
	req := &model.Request{
		Robots: []model.Robot{
			{ID: "r1", Start: p(0, 0), Battery: 5, BatteryCapacity: 5, ChargeRate: 1, PayloadCapacity: 0},
		},
	}
	resp := solveReq(req)
	if !resp.Feasible || resp.Makespan != 0 {
		t.Fatalf("empty tasks: got %+v", resp)
	}
}

// ---------------------------------------------------------------------------
// Exhaustive cross-check on many small randomly generated instances.
// ---------------------------------------------------------------------------

func TestRandomInstancesMatchBruteForce(t *testing.T) {
	rng := rand.New(rand.NewSource(20260924))
	for iter := 0; iter < 60; iter++ {
		req := randomInstance(rng)
		epd, err := req.Validate()
		if err != nil {
			t.Fatalf("iter %d: generator made invalid request: %v", iter, err)
		}
		resp := solver.Solve(req, epd)
		ok, opt := refOptimal(req, epd)
		if !ok {
			if resp.Feasible {
				t.Fatalf("iter %d: solver claims feasible but brute force says infeasible", iter)
			}
			if resp.Reason == "" {
				t.Fatalf("iter %d: infeasible response without reason", iter)
			}
			continue
		}
		if !resp.Feasible {
			t.Fatalf("iter %d: solver says infeasible but brute force optimum is %d\n%+v", iter, opt, req)
		}
		if resp.Makespan != opt {
			t.Fatalf("iter %d: solver makespan %d != brute force %d", iter, resp.Makespan, opt)
		}
		verifyPlan(t, req, resp, opt)
	}
}

func randomInstance(rng *rand.Rand) *model.Request {
	nR := 1 + rng.Intn(2)
	nT := rng.Intn(4) // 0..3 tasks
	nC := rng.Intn(2)
	req := &model.Request{}
	for i := 0; i < nR; i++ {
		cap := int64(12 + rng.Intn(14))
		req.Robots = append(req.Robots, model.Robot{
			ID:              fmt.Sprintf("r%d", i+1),
			Start:           p(int64(rng.Intn(7)), int64(rng.Intn(7))),
			Battery:         int64(rng.Intn(int(cap) + 1)),
			BatteryCapacity: cap,
			ChargeRate:      int64(2 + rng.Intn(6)),
			PayloadCapacity: int64(1 + rng.Intn(4)),
		})
	}
	for i := 0; i < nT; i++ {
		ready := int64(rng.Intn(10))
		req.Tasks = append(req.Tasks, model.Task{
			ID:      fmt.Sprintf("j%d", i+1),
			Loc:     p(int64(rng.Intn(11)), int64(rng.Intn(11))),
			Payload: int64(rng.Intn(4)),
			Ready:   ready,
			Due:     ready + int64(rng.Intn(16)),
			Service: int64(rng.Intn(3)),
		})
	}
	for i := 0; i < nC; i++ {
		req.Chargers = append(req.Chargers, model.Charger{
			ID:  fmt.Sprintf("c%d", i+1),
			Loc: p(int64(rng.Intn(11)), int64(rng.Intn(11))),
		})
	}
	return req
}
