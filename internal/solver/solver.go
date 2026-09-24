// Package solver 用深度优先分支定界求解离线多机器人任务分配，
// 目标是最小化总完工时间(makespan，最后一台机器人完成最后一个服务的时间)。
//
// 搜索约定(与 validate 重放一致)：
//   - 机器人始终在空载状态下移动；每次决策对应动作序列
//     [途中充电点充满电]* -> depot 取货 -> [途中充电点充满电]* -> 任务点送达；
//   - 到达任务点早于 Ready 则等待；服务必须在 [Ready,Due] 内开始；
//   - 取货不等待(depot 无时间窗)，到达即可取；
//   - 到达任务点后不再移动去充电(送达即完成本任务)。
//
// 搜索在每个状态枚举“下一个立即执行的 (任务, 机器人, 充电路径方案)”全部分支，
// 因而覆盖任务到机器人的全部分配与全部服务顺序；分支按 Due、完成时间排序，
// 仅用于尽早获得好的上界。用当前最优 makespan 剪枝，并对(规格,当前状态)
// 完全相同的机器人做同构剪枝。路径方案由 Pareto 标签搜索给出(见 route 包)。
package solver

import (
	"sort"
	"strconv"

	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/route"
)

// DefaultNodeLimit 是默认的搜索节点上限。
const DefaultNodeLimit int64 = 200000

type rstate struct {
	loc     model.Point
	t       int64 // 当前时间
	battery int64 // 当前电量
}

// decision 记录一次分支选择，供最优解重建。
type decision struct {
	robotIdx  int
	taskIdx   int
	pickupOpt route.Option // 当前位置 -> depot 的非劣路径
	deliver   route.Option // depot -> 任务点的非劣路径
	finishT   int64        // 完成服务后的时间
}

type solver struct {
	p         *model.Problem
	nR, nT    int
	deadline  int64 // 路径到达时间裁剪上界(= 当前最优 makespan)
	nodeLimit int64

	nodes    int64
	limitHit bool

	states []rstate
	curMax int64

	// 最优解
	bestFeasible bool
	bestMakespan int64
	bestLog      []decision

	log []decision
}

type action struct {
	pickup  route.Option
	deliver route.Option
	finish  int64
}

// Solve 求最优可行计划。
func Solve(p *model.Problem, nodeLimit int64) model.Result {
	if nodeLimit <= 0 {
		nodeLimit = DefaultNodeLimit
	}
	s := &solver{
		p:            p,
		nR:           len(p.Robots),
		nT:           len(p.Tasks),
		nodeLimit:    nodeLimit,
		states:       make([]rstate, len(p.Robots)),
		bestMakespan: 1 << 62,
	}
	s.resetStates()

	// 快速不可行判定：任务载荷超过所有机器人容量。
	for _, t := range p.Tasks {
		capable := false
		for _, r := range p.Robots {
			if r.Capacity >= t.Load {
				capable = true
				break
			}
		}
		if !capable {
			return model.Result{
				Feasible:  false,
				Optimal:   true,
				Reason:    "任务 " + t.ID + " 的载荷超过任何机器人的容量",
				NodeLimit: nodeLimit,
			}
		}
	}

	// 贪心初始上界：每步挑“最早可完成”的 (机器人,任务) 组合。
	gLog, gFeasible := s.greedy()
	if gFeasible {
		s.bestFeasible = true
		s.bestMakespan = logMakespan(gLog)
		s.bestLog = append([]decision(nil), gLog...)
	}
	// 贪心过程改动过 states，搜索前复位。
	s.resetStates()
	s.deadline = s.bestMakespan // 贪心不可行时为 1<<62，即不按上界裁剪

	s.dfs(0)

	// 没有任何可行解：若触碰上限则无法下结论，否则为已证无解。
	if !s.bestFeasible {
		if s.limitHit {
			return model.Result{
				Feasible:      false,
				Optimal:       false,
				Reason:        "搜索达到节点上限，未能确定可行性",
				NodesSearched: s.nodes,
				NodeLimit:     nodeLimit,
			}
		}
		return model.Result{
			Feasible:      false,
			Optimal:       true,
			Reason:        "穷举全部可行分配/顺序/充电路径后无解：存在时间窗、电量或容量约束无法同时满足",
			NodesSearched: s.nodes,
			NodeLimit:     nodeLimit,
		}
	}

	plans := buildPlans(p, s.bestLog, s.nR)
	res := model.Result{
		Feasible:      true,
		Optimal:       !s.limitHit,
		Makespan:      s.bestMakespan,
		Plans:         plans,
		NodesSearched: s.nodes,
		NodeLimit:     nodeLimit,
	}
	if s.limitHit {
		res.Reason = "搜索达到节点上限：返回的是可行解但未证明最优"
	}
	return res
}

func (s *solver) resetStates() {
	for i, r := range s.p.Robots {
		s.states[i] = rstate{loc: r.Start, t: 0, battery: r.StartBattery}
	}
}

// feasibleActions 计算机器人 ri 从当前状态服务任务 ti 的所有方案，
// 每个方案包含“到 depot”和“depot 到任务”的两段非劣路径。
func (s *solver) feasibleActions(ri, ti int) []action {
	r := &s.p.Robots[ri]
	t := &s.p.Tasks[ti]
	st := &s.states[ri]
	if t.Load > r.Capacity {
		return nil // 单件货物即超容量
	}

	var acts []action
	// 第一段：当前位置 -> depot(到达即可取货)。
	pickups := route.ReachOptions(
		st.loc, st.t, st.battery,
		s.p.Depot.Location, r.BatteryCapacity, r.EnergyPerDist, r.ChargeRate,
		s.deadline, 0, s.p.Chargers,
	)
	for _, po := range pickups {
		pickupEnd := po.ArrivalTime // depot 无服务耗时
		bat := po.ArrivalBattery
		// 第二段：depot -> 任务点；到达时间不得晚于 Due(晚到无法在窗内服务)。
		deliveries := route.ReachOptions(
			s.p.Depot.Location, pickupEnd, bat,
			t.Location, r.BatteryCapacity, r.EnergyPerDist, r.ChargeRate,
			t.Due, 0, s.p.Chargers,
		)
		for _, dOpt := range deliveries {
			svcStart := dOpt.ArrivalTime
			if svcStart < t.Ready {
				svcStart = t.Ready
			}
			finish := svcStart + t.ServiceTime
			if finish >= s.bestMakespan {
				continue // 无法严格改进当前最优
			}
			acts = append(acts, action{pickup: po, deliver: dOpt, finish: finish})
		}
	}
	sort.Slice(acts, func(i, j int) bool { return acts[i].finish < acts[j].finish })
	return acts
}

func (s *solver) dfs(depth int) {
	if s.nodes >= s.nodeLimit {
		s.limitHit = true
		return
	}
	s.nodes++

	if depth == s.nT {
		if s.curMax < s.bestMakespan {
			s.bestFeasible = true
			s.bestMakespan = s.curMax
			s.bestLog = append(s.bestLog[:0], s.log...)
			s.deadline = s.bestMakespan
		}
		return
	}

	// 分支必须覆盖“下一个服务哪个任务”的全部可能：任务顺序本身就是
	// 排列搜索的一部分，且执行其他任务会改变机器人位置/时间/电量，
	// 当前不可达的任务在其他任务之后可能变为可达(途中充满电)，
	// 因此不能只对单个 MRV 任务分支。
	branches := s.allBranches()

	for _, b := range branches {
		ri := b.ri
		ti := b.ti
		act := b.act
		old := s.states[ri]
		s.states[ri] = rstate{
			loc:     s.p.Tasks[ti].Location,
			t:       act.finish,
			battery: act.deliver.ArrivalBattery,
		}
		prevMax := s.curMax
		if act.finish > s.curMax {
			s.curMax = act.finish
		}
		s.log = append(s.log, decision{
			robotIdx:  ri,
			taskIdx:   ti,
			pickupOpt: act.pickup,
			deliver:   act.deliver,
			finishT:   act.finish,
		})

		s.dfs(depth + 1)

		s.log = s.log[:len(s.log)-1]
		s.curMax = prevMax
		s.states[ri] = old
	}
}

type branch struct {
	ri  int
	ti  int
	act action
}

// allBranches 枚举当前状态下全部可立即执行的 (任务, 机器人, 路径方案)。
// 排序仅为尽早找到好解、加强上界剪枝，不影响完备性：
// Due 越早的任务越优先，同任务下完成时间越早的方案越优先。
// (规格,当前状态)完全相同的机器人是同构的，同一方案只展开第一台。
func (s *solver) allBranches() []branch {
	assigned := s.assignedMask()
	var branches []branch
	for ti := 0; ti < s.nT; ti++ {
		if assigned&(1<<ti) != 0 {
			continue
		}
		seenClass := map[string]bool{}
		for ri := 0; ri < s.nR; ri++ {
			sig := robotSig(s.p, s.states, ri)
			if seenClass[sig] {
				continue
			}
			seenClass[sig] = true
			for _, a := range s.feasibleActions(ri, ti) {
				branches = append(branches, branch{ri: ri, ti: ti, act: a})
			}
		}
	}
	sort.Slice(branches, func(i, j int) bool {
		di, dj := s.p.Tasks[branches[i].ti].Due, s.p.Tasks[branches[j].ti].Due
		if di != dj {
			return di < dj
		}
		if branches[i].act.finish != branches[j].act.finish {
			return branches[i].act.finish < branches[j].act.finish
		}
		if branches[i].ti != branches[j].ti {
			return branches[i].ti < branches[j].ti
		}
		return branches[i].ri < branches[j].ri
	})
	return branches
}

func (s *solver) assignedMask() uint64 {
	var m uint64
	for _, d := range s.log {
		m |= 1 << d.taskIdx
	}
	return m
}

// robotSig 生成机器人(规格, 当前状态)的签名，用于同构剪枝；不含机器人 ID。
func robotSig(p *model.Problem, states []rstate, ri int) string {
	r := &p.Robots[ri]
	st := &states[ri]
	return strconv.FormatInt(r.BatteryCapacity, 10) + "," +
		strconv.FormatInt(r.Capacity, 10) + "," +
		strconv.FormatInt(r.EnergyPerDist, 10) + "," +
		strconv.FormatInt(r.ChargeRate, 10) + "|" +
		strconv.FormatInt(st.loc.X, 10) + "," + strconv.FormatInt(st.loc.Y, 10) + "," +
		strconv.FormatInt(st.t, 10) + "," + strconv.FormatInt(st.battery, 10)
}

// greedy 贪心构造一个可行解(若存在)：每步选择最早完成的 (机器人,任务)。
func (s *solver) greedy() ([]decision, bool) {
	savedDeadline, savedBest := s.deadline, s.bestMakespan
	s.deadline, s.bestMakespan = 1<<62, 1<<62
	defer func() { s.deadline, s.bestMakespan = savedDeadline, savedBest }()

	states := make([]rstate, s.nR)
	for i, r := range s.p.Robots {
		states[i] = rstate{loc: r.Start, t: 0, battery: r.StartBattery}
	}
	log := make([]decision, 0, s.nT)
	done := make([]bool, s.nT)

	for assigned := 0; assigned < s.nT; assigned++ {
		var bestRI, bestTI int
		var bestAct action
		found := false
		for ti := 0; ti < s.nT; ti++ {
			if done[ti] {
				continue
			}
			for ri := 0; ri < s.nR; ri++ {
				s.states[ri] = states[ri]
				acts := s.feasibleActions(ri, ti)
				if len(acts) > 0 && (!found || acts[0].finish < bestAct.finish) {
					found, bestRI, bestTI, bestAct = true, ri, ti, acts[0]
				}
			}
		}
		if !found {
			return nil, false
		}
		done[bestTI] = true
		states[bestRI] = rstate{
			loc:     s.p.Tasks[bestTI].Location,
			t:       bestAct.finish,
			battery: bestAct.deliver.ArrivalBattery,
		}
		log = append(log, decision{
			robotIdx:  bestRI,
			taskIdx:   bestTI,
			pickupOpt: bestAct.pickup,
			deliver:   bestAct.deliver,
			finishT:   bestAct.finish,
		})
	}
	return log, true
}

func logMakespan(log []decision) int64 {
	var m int64
	for _, d := range log {
		if d.finishT > m {
			m = d.finishT
		}
	}
	return m
}

// buildPlans 把决策日志重放为每台机器人的动作序列。
func buildPlans(p *model.Problem, log []decision, nR int) []model.RobotPlan {
	byRobot := make([][]decision, nR)
	for _, d := range log {
		byRobot[d.robotIdx] = append(byRobot[d.robotIdx], d)
	}

	plans := make([]model.RobotPlan, nR)
	for ri := 0; ri < nR; ri++ {
		r := &p.Robots[ri]
		plans[ri].RobotID = r.ID
		cur := r.Start
		curTime := int64(0)
		curBat := r.StartBattery

		for _, d := range byRobot[ri] {
			t := &p.Tasks[d.taskIdx]
			// 第一段：当前位置 -> depot，途中充电点产生 charge 动作。
			curTime, curBat = emitHops(r, &plans[ri], cur, curTime, curBat, d.pickupOpt.Path)
			cur = p.Depot.Location
			plans[ri].Actions = append(plans[ri].Actions, model.Action{
				RobotID:       r.ID,
				Type:          "pickup",
				TaskID:        t.ID,
				From:          cur,
				To:            cur,
				StartTime:     curTime,
				ArrivalTime:   curTime,
				EndTime:       curTime,
				BatteryBefore: curBat,
				BatteryAfter:  curBat,
			})
			// 第二段：depot -> 任务点，途中充电点产生 charge 动作。
			arrive, batAfter := emitHops(r, &plans[ri], cur, curTime, curBat, d.deliver.Path)
			cur = t.Location
			curTime = arrive
			svcStart := arrive
			if svcStart < t.Ready {
				svcStart = t.Ready
			}
			plans[ri].Actions = append(plans[ri].Actions, model.Action{
				RobotID:       r.ID,
				Type:          "service",
				TaskID:        t.ID,
				From:          cur,
				To:            cur,
				StartTime:     curTime,
				ArrivalTime:   arrive,
				ServiceTime:   t.ServiceTime, // 不含等待
				EndTime:       svcStart + t.ServiceTime,
				BatteryBefore: batAfter,
				BatteryAfter:  batAfter,
			})
			curTime = d.finishT
			curBat = batAfter
		}
		plans[ri].Makespan = curTime
	}
	return plans
}

// emitHops 把一条非劣路径的各段动作写入 plan，
// 返回到达终点时的(时间, 电量)。hops[0] 为起点(即 cur)：
// Charged 的充电跳产生单个 charge 动作；其余每跳产生一个 travel 动作。
func emitHops(
	r *model.Robot, plan *model.RobotPlan,
	start model.Point, startTime, startBat int64,
	hops []route.Hop,
) (int64, int64) {
	cur := start
	curTime := startTime
	curBat := startBat
	for k := 1; k < len(hops); k++ {
		h := hops[k]
		dist := model.Dist(cur, h.Point)
		arrive := curTime + dist
		driveBat := curBat - dist*r.EnergyPerDist

		if h.Charged {
			if dist == 0 {
				// 已在充电点上：仅充满，不产生零长度行驶。
				chargeTime := (r.BatteryCapacity - curBat) * r.ChargeRate
				endT := curTime + chargeTime
				plan.Actions = append(plan.Actions, model.Action{
					RobotID:       r.ID,
					Type:          "charge",
					ChargerID:     h.ChargerID,
					From:          cur,
					To:            h.Point,
					StartTime:     curTime,
					TravelTime:    0,
					ArrivalTime:   curTime,
					ServiceTime:   chargeTime,
					EndTime:       endT,
					BatteryBefore: curBat,
					BatteryAfter:  r.BatteryCapacity,
				})
				curBat = r.BatteryCapacity
				curTime = endT
				cur = h.Point
				continue
			}
			// 行驶到充电点并充满(行驶与充电合并为一个 charge 动作)。
			chargeTime := (r.BatteryCapacity - driveBat) * r.ChargeRate
			endT := arrive + chargeTime
			plan.Actions = append(plan.Actions, model.Action{
				RobotID:       r.ID,
				Type:          "charge",
				ChargerID:     h.ChargerID,
				From:          cur,
				To:            h.Point,
				StartTime:     curTime,
				TravelTime:    dist,
				ArrivalTime:   arrive,
				ServiceTime:   chargeTime,
				EndTime:       endT,
				BatteryBefore: curBat,
				BatteryAfter:  r.BatteryCapacity,
			})
			curBat = r.BatteryCapacity
			curTime = endT
		} else if dist > 0 {
			plan.Actions = append(plan.Actions, model.Action{
				RobotID:       r.ID,
				Type:          "travel",
				From:          cur,
				To:            h.Point,
				StartTime:     curTime,
				TravelTime:    dist,
				ArrivalTime:   arrive,
				EndTime:       arrive,
				BatteryBefore: curBat,
				BatteryAfter:  driveBat,
			})
			curBat = driveBat
			curTime = arrive
		}
		cur = h.Point
	}
	return curTime, curBat
}
