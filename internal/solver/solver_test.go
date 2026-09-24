package solver_test

import (
	"testing"

	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/solver"
	"github.com/example/robot-task/internal/validate"
)

// 核验一份可行结果：独立校验器通过、makespan 与计划一致。
func assertValid(t *testing.T, p *model.Problem, res model.Result, wantOptimal bool) {
	t.Helper()
	if !res.Feasible {
		t.Fatalf("期望可行，实际无解: %s", res.Reason)
	}
	if wantOptimal && !res.Optimal {
		t.Fatalf("期望已证明最优，实际 optimal=false (nodes=%d)", res.NodesSearched)
	}
	rep := validate.Check(p, res.Plans)
	if !rep.Valid {
		t.Fatalf("计划未通过独立校验: %v", rep.Errors)
	}
	if rep.Makespan != res.Makespan {
		t.Fatalf("重放 makespan %d != 求解器上报 %d", rep.Makespan, res.Makespan)
	}
	// 每段动作结束后电量非负。
	for _, pl := range res.Plans {
		for _, a := range pl.Actions {
			if a.BatteryAfter < 0 {
				t.Fatalf("机器人 %s 存在负电量段 %d", a.RobotID, a.BatteryAfter)
			}
		}
	}
}

func mustPoint(p model.Point, x, y int64) model.Point { return p }

// TestMustChargeBeforeTask：不充电就无法到达任务，必须先去充电。
func TestMustChargeBeforeTask(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 10, BatteryCapacity: 20,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 5, Y: 0}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 15, Y: 0},
			Load: 1, Ready: 0, Due: 1000, ServiceTime: 0,
		}},
	}
	res := solver.Solve(p, 0)
	assertValid(t, p, res, true)

	// 物理核对：depot(0,0)->charger(5,0) 用 5 电，充到 20 需 15 时间，
	// 再到任务(15,0) 距离 10，到达时刻 5+15+10=30。
	if res.Makespan != 30 {
		t.Fatalf("必须先充电场景 makespan 应为 30，实际 %d", res.Makespan)
	}
	var sawCharge bool
	for _, a := range res.Plans[0].Actions {
		if a.Type == "charge" && a.ChargerID == "c1" {
			sawCharge = true
			if a.BatteryBefore-a.TravelTime*1 < 0 {
				t.Fatal("充电段行驶后电量为负")
			}
		}
	}
	if !sawCharge {
		t.Fatal("计划中缺少充电动作")
	}
}

// TestTightWindowInfeasible：时间窗太紧，即使先充满电也赶不到 -> 明确无解。
func TestTightWindowInfeasible(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 20, BatteryCapacity: 20,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 0, Y: 5}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 20, Y: 0},
			Load: 1, Ready: 0, Due: 15, ServiceTime: 0, // 直达就要 20 > 15
		}},
	}
	res := solver.Solve(p, 0)
	if res.Feasible {
		t.Fatalf("期望因时间窗无解，实际给出计划(makespan=%d)", res.Makespan)
	}
	if !res.Optimal {
		// 小实例必须给出确定性结论而非触碰节点上限。
		t.Fatalf("无界搜索应给出已证无解，optimal=false: %s", res.Reason)
	}
}

// TestTightWindowFeasibleWithExactArrival：恰好压线到达(到达==due)可行。
func TestTightWindowFeasibleWithExactArrival(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 20, BatteryCapacity: 20,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 30, Y: 0}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 10, Y: 0},
			Load: 1, Ready: 0, Due: 10, ServiceTime: 0,
		}},
	}
	res := solver.Solve(p, 0)
	assertValid(t, p, res, true)
	if res.Makespan != 10 {
		t.Fatalf("压线到达 makespan 应为 10，实际 %d", res.Makespan)
	}
}

// TestTwoRobotsOptimalMakespan：两台机器人比一台更早完工，验证分配枚举。
func TestTwoRobotsOptimalMakespan(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{
			{ID: "r1", Start: model.Point{X: 0, Y: 0}, StartBattery: 50, BatteryCapacity: 50,
				Capacity: 10, EnergyPerDist: 1, ChargeRate: 1},
			{ID: "r2", Start: model.Point{X: 0, Y: 0}, StartBattery: 50, BatteryCapacity: 50,
				Capacity: 10, EnergyPerDist: 1, ChargeRate: 1},
		},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 50, Y: 50}}},
		Tasks: []model.Task{
			{ID: "t1", Location: model.Point{X: 8, Y: 0}, Load: 1, Ready: 0, Due: 100, ServiceTime: 0},
			{ID: "t2", Location: model.Point{X: -10, Y: 0}, Load: 1, Ready: 0, Due: 100, ServiceTime: 0},
		},
	}
	res := solver.Solve(p, 0)
	assertValid(t, p, res, true)
	// 各跑一个：max(8,10)=10；一台连跑需 8+(8+10)=26。
	if res.Makespan != 10 {
		t.Fatalf("两机并行最优 makespan 应为 10，实际 %d", res.Makespan)
	}
	counts := map[string]int{}
	for _, pl := range res.Plans {
		for _, a := range pl.Actions {
			if a.Type == "service" {
				counts[a.TaskID]++
			}
		}
	}
	for _, id := range []string{"t1", "t2"} {
		if counts[id] != 1 {
			t.Fatalf("任务 %s 被服务 %d 次，应为恰好 1 次", id, counts[id])
		}
	}
}

// TestCapacityInfeasible：容量约束导致无解。
func TestCapacityInfeasible(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 50, BatteryCapacity: 50,
			Capacity: 3, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 50, Y: 0}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 5, Y: 0},
			Load: 5, Ready: 0, Due: 100, ServiceTime: 0,
		}},
	}
	res := solver.Solve(p, 0)
	if res.Feasible || !res.Optimal {
		t.Fatalf("期望容量不足导致已证无解，实际 feasible=%v optimal=%v", res.Feasible, res.Optimal)
	}
}

// TestWaitingAtTask：早到必须等待到 ready，makespan 含等待时间。
func TestWaitingAtTask(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 20, BatteryCapacity: 20,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 20, Y: 20}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 4, Y: 0},
			Load: 1, Ready: 50, Due: 100, ServiceTime: 3,
		}},
	}
	res := solver.Solve(p, 0)
	assertValid(t, p, res, true)
	// 4 时刻到达，等到 50，服务 3 -> 53
	if res.Makespan != 53 {
		t.Fatalf("等待场景 makespan 应为 53，实际 %d", res.Makespan)
	}
}

// TestChargeWithServiceTime：充电路径 + 服务耗时联合核对。
func TestChargeWithServiceTime(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 10, BatteryCapacity: 20,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 2,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 5, Y: 0}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 15, Y: 0},
			Load: 1, Ready: 0, Due: 1000, ServiceTime: 4,
		}},
	}
	res := solver.Solve(p, 0)
	assertValid(t, p, res, true)
	// 5 行驶 + 15*2 充电 + 10 行驶 + 4 服务 = 49
	if res.Makespan != 49 {
		t.Fatalf("充电+服务场景 makespan 应为 49，实际 %d", res.Makespan)
	}
}
