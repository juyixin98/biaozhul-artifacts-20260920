// Package validate 独立地重放一份计划，逐条核验物理约束，
// 供服务自检和测试使用。校验内容：
//   - 每个动作的机器人存在、位置衔接、旅行时间与耗能正确；
//   - 每段行驶后电量非负，充电后不超过容量、充电耗时计算正确；
//   - pickup 后载荷不超过容量；service 前该机器人已取货；
//   - 服务在时间窗 [Ready, Due] 内开始，等待时长非负；
//   - 动作时间单调不回退；每台机器人恰好一条计划；
//   - 每个任务恰好被取货一次、服务一次且由同一机器人完成。
package validate

import (
	"fmt"

	"github.com/example/robot-task/internal/model"
)

// Report 是一次校验的结果。
type Report struct {
	Valid    bool     `json:"valid"`
	Errors   []string `json:"errors,omitempty"`
	Makespan int64    `json:"makespan"`
}

// Check 重放 plans 并核验其对问题 p 是否可行。
func Check(p *model.Problem, plans []model.RobotPlan) Report {
	var errs []string
	add := func(format string, args ...any) {
		errs = append(errs, fmt.Sprintf(format, args...))
	}

	robots := map[string]*model.Robot{}
	for i := range p.Robots {
		robots[p.Robots[i].ID] = &p.Robots[i]
	}
	tasks := map[string]*model.Task{}
	for i := range p.Tasks {
		tasks[p.Tasks[i].ID] = &p.Tasks[i]
	}
	chargers := map[string]*model.Charger{}
	for i := range p.Chargers {
		chargers[p.Chargers[i].ID] = &p.Chargers[i]
	}

	pickupCount := map[string]int{}
	serviceCount := map[string]int{}
	seenRobot := map[string]bool{}
	var makespan int64

	if len(plans) != len(p.Robots) {
		add("计划条目数 %d 与机器人数 %d 不一致", len(plans), len(p.Robots))
	}

	for pi := range plans {
		plan := &plans[pi]
		rb, ok := robots[plan.RobotID]
		if !ok {
			add("计划引用了不存在的机器人 %q", plan.RobotID)
			continue
		}
		if seenRobot[plan.RobotID] {
			add("机器人 %q 出现多条计划", plan.RobotID)
		}
		seenRobot[plan.RobotID] = true

		cur := rb.Start
		curTime := int64(0)
		curBattery := rb.StartBattery
		var load int64
		carrying := map[string]bool{}
		var planEnd int64

		for ai := range plan.Actions {
			a := &plan.Actions[ai]
			if a.RobotID != plan.RobotID {
				add("机器人 %q 的计划中混入了 %q 的动作", plan.RobotID, a.RobotID)
			}
			if a.From != cur {
				add("机器人 %q 第 %d 段起点(%d,%d)与上一段终点(%d,%d)不符",
					plan.RobotID, ai, a.From.X, a.From.Y, cur.X, cur.Y)
			}
			if a.StartTime < curTime {
				add("机器人 %q 第 %d 段开始时间 %d 早于上段结束 %d",
					plan.RobotID, ai, a.StartTime, curTime)
			}
			if a.BatteryBefore != curBattery {
				add("机器人 %q 第 %d 段起始电量 %d 与重放值 %d 不符",
					plan.RobotID, ai, a.BatteryBefore, curBattery)
			}

			d := model.Dist(a.From, a.To)
			switch a.Type {
			case "travel":
				if a.TravelTime != d {
					add("机器人 %q 第 %d 段旅行时间 %d 与距离 %d 不符",
						plan.RobotID, ai, a.TravelTime, d)
				}
				if a.ArrivalTime != a.StartTime+a.TravelTime {
					add("机器人 %q 第 %d 段到达时间计算错误", plan.RobotID, ai)
				}
				used := d * rb.EnergyPerDist
				if used > a.BatteryBefore {
					add("机器人 %q 第 %d 段耗电 %d 超过出发前电量 %d（行驶后电量为负）",
						plan.RobotID, ai, used, a.BatteryBefore)
				}
				if a.BatteryAfter != a.BatteryBefore-used {
					add("机器人 %q 第 %d 段行驶后电量应为 %d，实际 %d",
						plan.RobotID, ai, a.BatteryBefore-used, a.BatteryAfter)
				}
				if a.EndTime != a.ArrivalTime {
					add("机器人 %q 第 %d 段 travel 不应有停留", plan.RobotID, ai)
				}
				if a.ServiceTime != 0 {
					add("机器人 %q 第 %d 段 travel 的 service_time 应为 0", plan.RobotID, ai)
				}
				curBattery = a.BatteryAfter
				curTime = a.EndTime

			case "charge":
				c, ok := chargers[a.ChargerID]
				if !ok {
					add("机器人 %q 在不存在的充电点 %q 充电", plan.RobotID, a.ChargerID)
				} else if a.To != c.Location {
					add("机器人 %q 充电终点与充电点 %q 位置不符", plan.RobotID, a.ChargerID)
				}
				if a.TravelTime != d {
					add("机器人 %q 充电段旅行时间 %d 与距离 %d 不符", plan.RobotID, ai, a.TravelTime, d)
				}
				if a.ArrivalTime != a.StartTime+a.TravelTime {
					add("机器人 %q 充电段到达时间计算错误", plan.RobotID)
				}
				used := d * rb.EnergyPerDist
				if used > a.BatteryBefore {
					add("机器人 %q 开到充电点 %q 需要 %d 电，仅有 %d（行驶后电量为负）",
						plan.RobotID, a.ChargerID, used, a.BatteryBefore)
				}
				driveRem := a.BatteryBefore - used
				chargeAmt := a.BatteryAfter - driveRem
				if chargeAmt < 0 {
					add("机器人 %q 充电后电量 %d 低于到达时电量 %d", plan.RobotID, a.BatteryAfter, driveRem)
				}
				if a.BatteryAfter > rb.BatteryCapacity {
					add("机器人 %q 充电后电量 %d 超过容量 %d",
						plan.RobotID, a.BatteryAfter, rb.BatteryCapacity)
				}
				wantChargeTime := chargeAmt * rb.ChargeRate
				if a.ServiceTime != wantChargeTime {
					add("机器人 %q 充电段 service_time %d 与按充入电量 %d*rate 计算的 %d 不符",
						plan.RobotID, a.ServiceTime, chargeAmt, wantChargeTime)
				}
				if a.EndTime != a.ArrivalTime+wantChargeTime {
					add("机器人 %q 充电段结束时间计算错误", plan.RobotID)
				}
				curBattery = a.BatteryAfter
				curTime = a.EndTime

			case "pickup":
				t, ok := tasks[a.TaskID]
				if !ok {
					add("机器人 %q pickup 不存在任务 %q", plan.RobotID, a.TaskID)
					break
				}
				if a.From != p.Depot.Location || a.To != p.Depot.Location {
					add("机器人 %q pickup %q 不在 depot", plan.RobotID, a.TaskID)
				}
				if a.TravelTime != 0 || a.ArrivalTime != a.StartTime || a.EndTime != a.StartTime {
					add("机器人 %q pickup %q 应为零长度事件", plan.RobotID, a.TaskID)
				}
				if a.BatteryAfter != a.BatteryBefore {
					add("机器人 %q pickup %q 不应改变电量", plan.RobotID, a.TaskID)
				}
				if carrying[a.TaskID] {
					add("机器人 %q 重复取货 %q", plan.RobotID, a.TaskID)
				}
				carrying[a.TaskID] = true
				pickupCount[a.TaskID]++
				load += t.Load
				if load > rb.Capacity {
					add("机器人 %q 取货 %q 后载荷 %d 超过容量 %d",
						plan.RobotID, a.TaskID, load, rb.Capacity)
				}
				curTime = a.EndTime

			case "service":
				t, ok := tasks[a.TaskID]
				if !ok {
					add("机器人 %q 服务不存在任务 %q", plan.RobotID, a.TaskID)
					break
				}
				if a.From != t.Location || a.To != t.Location {
					add("机器人 %q 服务 %q 的位置与任务位置不符", plan.RobotID, a.TaskID)
				}
				if !carrying[a.TaskID] {
					add("机器人 %q 服务任务 %q 前未由该机器人取货", plan.RobotID, a.TaskID)
				}
				if a.TravelTime != 0 || a.ArrivalTime != a.StartTime {
					add("机器人 %q 服务 %q 应为零行驶事件", plan.RobotID, a.TaskID)
				}
				if a.BatteryAfter != a.BatteryBefore {
					add("机器人 %q 服务 %q 不应改变电量", plan.RobotID, a.TaskID)
				}
				if a.ServiceTime != t.ServiceTime {
					add("机器人 %q 服务任务 %q 耗时 %d 与定义 %d 不符",
						plan.RobotID, a.TaskID, a.ServiceTime, t.ServiceTime)
				}
				svcStart := a.EndTime - a.ServiceTime
				if svcStart < a.ArrivalTime {
					add("机器人 %q 服务任务 %q 的开始早于到达", plan.RobotID, a.TaskID)
				}
				if svcStart < t.Ready || svcStart > t.Due {
					add("机器人 %q 服务任务 %q 在 %d 开始，超出时间窗 [%d,%d]",
						plan.RobotID, a.TaskID, svcStart, t.Ready, t.Due)
				}
				delete(carrying, a.TaskID)
				serviceCount[a.TaskID]++
				load -= t.Load
				if load < 0 {
					add("机器人 %q 送达 %q 后载荷为负", plan.RobotID, a.TaskID)
				}
				curBattery = a.BatteryAfter
				curTime = a.EndTime

			default:
				add("机器人 %q 第 %d 段动作类型 %q 非法", plan.RobotID, ai, a.Type)
			}

			if a.BatteryAfter < 0 {
				add("机器人 %q 第 %d 段结束电量为负 %d", plan.RobotID, ai, a.BatteryAfter)
			}
			cur = a.To
			planEnd = curTime
		}

		if len(carrying) != 0 {
			add("机器人 %q 计划结束时仍有未送达货物", plan.RobotID)
		}
		if plan.Makespan != planEnd {
			add("机器人 %q 上报 makespan %d 与重放值 %d 不符",
				plan.RobotID, plan.Makespan, planEnd)
		}
		if planEnd > makespan {
			makespan = planEnd
		}
	}

	for _, t := range p.Tasks {
		if pickupCount[t.ID] != 1 {
			add("任务 %q 被取货 %d 次（应恰好 1 次）", t.ID, pickupCount[t.ID])
		}
		if serviceCount[t.ID] != 1 {
			add("任务 %q 被服务 %d 次（应恰好 1 次）", t.ID, serviceCount[t.ID])
		}
	}

	return Report{Valid: len(errs) == 0, Errors: errs, Makespan: makespan}
}
