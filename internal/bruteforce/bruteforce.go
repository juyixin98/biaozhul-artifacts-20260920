// Package bruteforce 是一个刻意朴素的独立穷举器，仅用于验收交叉核对，
// 不与 solver/route 共享任何搜索代码。
//
// 它枚举：每个任务分配给哪台机器人、每台机器人服务任务的顺序、
// 每段路程(去 depot、去任务点)是直达还是在某个充电点充满电。
// 对每个完整方案模拟时间窗、电量与容量，取最小 makespan。
//
// 适用前提(测试实例均满足)：任意两个相关点之间的单段距离耗能不超过满电电量，
// 因此每段至多在途中充一次电即可覆盖全部物理可行路线。
package bruteforce

import (
	"math"

	"github.com/example/robot-task/internal/model"
)

type state struct {
	loc     model.Point
	t       int64
	battery int64
}

type legOption struct {
	arrive     int64 // 到达终点的时间
	endBattery int64 // 到达终点时电量
}

// MinMakespan 返回穷举得到的最小 makespan；无可行方案时 ok=false。
func MinMakespan(p *model.Problem) (best int64, ok bool) {
	n := len(p.Tasks)
	states := make([]state, len(p.Robots))
	for i, r := range p.Robots {
		states[i] = state{loc: r.Start, t: 0, battery: r.StartBattery}
	}
	best = int64(math.MaxInt64)

	var dfs func(done uint64, depth int, curMax int64)
	dfs = func(done uint64, depth int, curMax int64) {
		if depth == n {
			if curMax < best {
				best = curMax
			}
			return
		}
		if curMax >= best {
			return
		}
		for ti := 0; ti < n; ti++ {
			if done&(1<<ti) != 0 {
				continue
			}
			task := &p.Tasks[ti]
			for ri := range p.Robots {
				robot := &p.Robots[ri]
				if task.Load > robot.Capacity {
					continue
				}
				st := states[ri]
				legsA := legOptions(robot, st.loc, p.Depot.Location, st.t, st.battery, p.Chargers)
				for _, legA := range legsA {
					pickupT := legA.arrive // depot 无服务耗时
					legsB := legOptions(robot, p.Depot.Location, task.Location, pickupT, legA.endBattery, p.Chargers)
					for _, legB := range legsB {
						if legB.arrive > task.Due {
							continue
						}
						svcStart := legB.arrive
						if svcStart < task.Ready {
							svcStart = task.Ready
						}
						finish := svcStart + task.ServiceTime
						if finish >= best {
							continue
						}
						newMax := curMax
						if finish > newMax {
							newMax = finish
						}
						old := states[ri]
						states[ri] = state{loc: task.Location, t: finish, battery: legB.endBattery}
						dfs(done|(1<<ti), depth+1, newMax)
						states[ri] = old
					}
				}
			}
		}
	}
	dfs(0, 0, 0)
	return best, best != int64(math.MaxInt64)
}

// legOptions 枚举从 from(时间 t0、电量 b0)到 to 的路线：
// 直达，或在某个充电点充满后再去 to。
func legOptions(
	r *model.Robot, from, to model.Point, t0, b0 int64,
	chargers []model.Charger,
) []legOption {
	var opts []legOption

	// 直达
	d0 := model.Dist(from, to)
	if d0*r.EnergyPerDist <= b0 {
		opts = append(opts, legOption{
			arrive:     t0 + d0,
			endBattery: b0 - d0*r.EnergyPerDist,
		})
	}

	// 经某充电点充满
	for _, c := range chargers {
		d1 := model.Dist(from, c.Location)
		if d1*r.EnergyPerDist > b0 {
			continue // 开不到充电点
		}
		arriveC := t0 + d1
		remC := b0 - d1*r.EnergyPerDist
		readyT := arriveC + (r.BatteryCapacity-remC)*r.ChargeRate
		d2 := model.Dist(c.Location, to)
		if d2*r.EnergyPerDist > r.BatteryCapacity {
			continue // 满电也开不到终点(该测试前提通常不触发)
		}
		opts = append(opts, legOption{
			arrive:     readyT + d2,
			endBattery: r.BatteryCapacity - d2*r.EnergyPerDist,
		})
	}
	return opts
}
