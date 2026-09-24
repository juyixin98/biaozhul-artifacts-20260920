// Package solver implements an exact offline multi-robot task allocator.
//
// Model (all quantities are integers):
//   - Locations are points on a 2-D grid; travel time between two points
//     equals their Manhattan distance (unit speed).
//   - Travel energy = energyPerDist * distance.
//   - Each task has a payload requirement and a hard time window
//     [ready, due]; service may start at any integer time t with
//     ready <= t <= due (robots wait if early) and lasts "service" units.
//   - A robot may make at most one detour to a charging point between two
//     consecutive task services; a charging stop restores the battery to
//     full in ceil((capacity-battery)/chargeRate) time. One stop is
//     sufficient because after a stop the battery is already full: a
//     second stop before reaching the next task cannot enable anything
//     the first one does not.
//
// The solver enumerates every feasible (robot, order, charging-stop)
// choice with depth-first branch-and-bound and returns the plan with
// minimum makespan (time at which the last task finishes), or reports
// infeasibility. Exhaustive enumeration is exact; instance sizes are
// capped (see model.Max*).
package solver

import (
	"fmt"
	"math"

	"robotdispatch/model"
)

// Solver holds the instance data for one solve.
type Solver struct {
	robots   []model.Robot
	tasks    []model.Task
	chargers []model.Charger
	epd      int64

	states []rstate
	plans  [][]model.Step

	bestMakespan int64
	bestPlans    [][]model.Step
	bestFinish   []int64
	nodes        int64
}

type rstate struct {
	loc model.Point
	t   int64 // time at which the robot becomes free
	bat int64 // battery level when free
}

// Solve runs the exact search. req is assumed validated; epd is the
// normalized energy-per-distance returned by Validate.
func Solve(req *model.Request, epd int64) *model.Response {
	s := &Solver{
		robots:       req.Robots,
		tasks:        req.Tasks,
		chargers:     req.Chargers,
		epd:          epd,
		states:       make([]rstate, len(req.Robots)),
		plans:        make([][]model.Step, len(req.Robots)),
		bestMakespan: math.MaxInt64,
	}
	for i, rb := range req.Robots {
		s.states[i] = rstate{loc: rb.Start, t: 0, bat: rb.Battery}
	}

	if len(req.Tasks) == 0 {
		return &model.Response{
			Feasible: true,
			Makespan: 0,
			Plans:    s.idlePlans(),
		}
	}

	// Cheap necessary-condition check: each task must fit some robot.
	for _, t := range req.Tasks {
		carrier := false
		for _, rb := range req.Robots {
			if rb.PayloadCapacity >= t.Payload {
				carrier = true
				break
			}
		}
		if !carrier {
			return &model.Response{
				Feasible: false,
				Reason:   "infeasible: task " + t.ID + " exceeds the payload capacity of every robot",
			}
		}
	}

	fullMask := uint16(1)<<uint(len(req.Tasks)) - 1
	s.dfs(0, fullMask)

	if s.bestPlans == nil {
		return &model.Response{
			Feasible: false,
			Reason:   "infeasible: no assignment satisfies all time windows with non-negative battery (all task orderings, robot assignments and charging detours exhausted)",
		}
	}

	plans := make([]model.RobotPlan, len(s.robots))
	for i, rb := range s.robots {
		steps := s.bestPlans[i]
		if steps == nil {
			steps = []model.Step{}
		}
		plans[i] = model.RobotPlan{
			RobotID:  rb.ID,
			Start:    rb.Start,
			Steps:    steps,
			FinishAt: s.bestFinish[i],
		}
	}
	return &model.Response{Feasible: true, Makespan: s.bestMakespan, Plans: plans}
}

func (s *Solver) idlePlans() []model.RobotPlan {
	plans := make([]model.RobotPlan, len(s.robots))
	for i, rb := range s.robots {
		plans[i] = model.RobotPlan{
			RobotID: rb.ID,
			Start:   rb.Start,
			Steps:   []model.Step{},
		}
	}
	return plans
}

// dfs extends every robot's route with any still-unassigned task.
// Enumerating (task, robot, charging-stop) triples at every node explores
// every route ordering for every robot: a complete sequence of such
// choices uniquely defines one full plan.
func (s *Solver) dfs(mask uint16, fullMask uint16) {
	s.nodes++
	if mask == fullMask {
		fin := s.finishLowerBound()
		if fin < s.bestMakespan {
			s.recordBest(fin)
		}
		return
	}

	// Lower bound: the makespan is at least the time the busiest robot is
	// free. Prune if it cannot beat the incumbent.
	if s.finishLowerBound() >= s.bestMakespan {
		return
	}

	// Consider more urgent tasks first so a tight incumbent (upper bound)
	// is found early and branch-and-bound prunes aggressively.
	order := s.taskOrder(mask)

	for _, k := range order {
		task := s.tasks[k]

		seenSig := map[string]bool{}
		for i, rb := range s.robots {
			if rb.PayloadCapacity < task.Payload {
				continue
			}
			st := s.states[i]
			// Robots with identical capabilities AND identical current state are
			// interchangeable; trying each duplicates the same subtree.
			sig := stateSig(rb, st)
			if seenSig[sig] {
				continue
			}
			seenSig[sig] = true

			// Candidate extensions, cheapest-looking first: direct service
			// before charging detours, nearer-free-time robots first by
			// construction of the robot scan order is not guaranteed, so the
			// branch-and-bound bound does the heavy lifting.

			// Option 1: go directly to the task.
			if ns, steps, ok := s.advance(rb, st, task, nil); ok {
				if ns.t < s.bestMakespan {
					s.apply(i, ns, steps)
					s.dfs(mask|1<<uint(k), fullMask)
					s.undo(i, st, 1)
				}
			}

			// Option 2: detour via one charging point first.
			for ci := range s.chargers {
				ch := &s.chargers[ci]
				if ns, steps, ok := s.advance(rb, st, task, ch); ok {
					if ns.t < s.bestMakespan {
						s.apply(i, ns, steps)
						s.dfs(mask|1<<uint(k), fullMask)
						s.undo(i, st, len(steps))
					}
				}
			}
		}
	}
}

// taskOrder returns unassigned task indices, tightest due time first.
func (s *Solver) taskOrder(mask uint16) []int {
	var out []int
	for k := range s.tasks {
		if mask&(1<<uint(k)) == 0 {
			out = append(out, k)
		}
	}
	// insertion sort (n <= MaxTasks)
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && s.tasks[out[j]].Due < s.tasks[out[j-1]].Due; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}

func (s *Solver) apply(i int, ns rstate, steps []model.Step) {
	s.states[i] = ns
	s.plans[i] = append(s.plans[i], steps...)
}

func (s *Solver) undo(i int, old rstate, added int) {
	s.states[i] = old
	s.plans[i] = s.plans[i][:len(s.plans[i])-added]
}

func (s *Solver) finishLowerBound() int64 {
	var m int64
	for _, st := range s.states {
		if st.t > m {
			m = st.t
		}
	}
	return m
}

func (s *Solver) recordBest(fin int64) {
	s.bestMakespan = fin
	s.bestPlans = make([][]model.Step, len(s.robots))
	s.bestFinish = make([]int64, len(s.robots))
	for i := range s.robots {
		s.bestFinish[i] = s.states[i].t
		if len(s.plans[i]) > 0 {
			cp := make([]model.Step, len(s.plans[i]))
			copy(cp, s.plans[i])
			s.bestPlans[i] = cp
		}
	}
}

func stateSig(rb model.Robot, st rstate) string {
	// Uniquely encode the robot's capabilities and current state. Robots
	// matching on all of these behave identically on the remaining tasks.
	return fmt.Sprintf("%d:%d:%d:%d:%d:%d:%d",
		rb.BatteryCapacity, rb.ChargeRate, rb.PayloadCapacity,
		st.loc.X, st.loc.Y, st.t, st.bat)
}

// advance simulates robot rb in state st serving task, optionally after a
// charging detour at ch. It returns the new state and the appended steps
// (one for direct service, two for a charging detour).
func (s *Solver) advance(rb model.Robot, st rstate, task model.Task, ch *model.Charger) (rstate, []model.Step, bool) {
	if ch == nil {
		d := model.Dist(st.loc, task.Loc)
		cost := s.epd * d
		if st.bat < cost {
			return rstate{}, nil, false
		}
		arrive := st.t + d
		batArr := st.bat - cost
		start := arrive
		if task.Ready > start {
			start = task.Ready
		}
		if start > task.Due {
			return rstate{}, nil, false
		}
		end := start + task.Service
		step := model.Step{
			Type:          "task",
			TaskID:        task.ID,
			From:          st.loc,
			To:            task.Loc,
			MoveDist:      d,
			MoveTime:      d,
			MoveCost:      cost,
			ArriveTime:    arrive,
			StartTime:     start,
			EndTime:       end,
			BatteryBefore: batArr,
			BatteryAfter:  batArr,
		}
		return rstate{loc: task.Loc, t: end, bat: batArr}, []model.Step{step}, true
	}

	// Leg 1: current position -> charger.
	d1 := model.Dist(st.loc, ch.Loc)
	cost1 := s.epd * d1
	if st.bat < cost1 {
		return rstate{}, nil, false
	}
	arriveCh := st.t + d1
	batAtCh := st.bat - cost1
	chargeTime := ceilDiv(rb.BatteryCapacity-batAtCh, rb.ChargeRate)
	chargeEnd := arriveCh + chargeTime

	// Leg 2: charger -> task.
	d2 := model.Dist(ch.Loc, task.Loc)
	cost2 := s.epd * d2
	if rb.BatteryCapacity < cost2 {
		return rstate{}, nil, false
	}
	arriveTask := chargeEnd + d2
	batArr := rb.BatteryCapacity - cost2
	start := arriveTask
	if task.Ready > start {
		start = task.Ready
	}
	if start > task.Due {
		return rstate{}, nil, false
	}
	end := start + task.Service

	chargeStep := model.Step{
		Type:          "charge",
		ChargerID:     ch.ID,
		From:          st.loc,
		To:            ch.Loc,
		MoveDist:      d1,
		MoveTime:      d1,
		MoveCost:      cost1,
		ArriveTime:    arriveCh,
		StartTime:     arriveCh,
		ChargeTime:    chargeTime,
		EndTime:       chargeEnd,
		BatteryBefore: batAtCh,
		BatteryAfter:  rb.BatteryCapacity,
	}
	taskStep := model.Step{
		Type:          "task",
		TaskID:        task.ID,
		From:          ch.Loc,
		To:            task.Loc,
		MoveDist:      d2,
		MoveTime:      d2,
		MoveCost:      cost2,
		ArriveTime:    arriveTask,
		StartTime:     start,
		EndTime:       end,
		BatteryBefore: batArr,
		BatteryAfter:  batArr,
	}
	return rstate{loc: task.Loc, t: end, bat: batArr}, []model.Step{chargeStep, taskStep}, true
}

func ceilDiv(a, b int64) int64 {
	if a <= 0 {
		return 0
	}
	return (a + b - 1) / b
}
