package solver_test

import (
	"fmt"
	"testing"

	"github.com/example/robot-task/internal/bruteforce"
	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/solver"
)

// TestNodeLimit：把节点上限压到极低，求解器必须诚实返回 optimal=false，
// 而不能谎称已证明最优或已证无解。
func TestNodeLimit(t *testing.T) {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots: []model.Robot{{
			ID: "r1", Start: model.Point{X: 0, Y: 0},
			StartBattery: 50, BatteryCapacity: 50,
			Capacity: 10, EnergyPerDist: 1, ChargeRate: 1,
		}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 50, Y: 50}}},
		Tasks: []model.Task{
			{ID: "t1", Location: model.Point{X: 1, Y: 0}, Load: 1, Ready: 0, Due: 1000, ServiceTime: 0},
			{ID: "t2", Location: model.Point{X: 2, Y: 0}, Load: 1, Ready: 0, Due: 1000, ServiceTime: 0},
			{ID: "t3", Location: model.Point{X: 3, Y: 0}, Load: 1, Ready: 0, Due: 1000, ServiceTime: 0},
		},
	}
	// 上限极小：DFS 无法完成，optimal 必须为 false。
	// 贪心阶段(不计入穷举节点)可能已给出可行上界，因此 feasible 可真可假；
	// 无论如何都不得谎称已证明最优。
	res := solver.Solve(p, 2)
	if res.Optimal {
		t.Fatal("节点上限内无法完成搜索时 optimal 必须为 false")
	}
	if res.Feasible {
		// 有贪心上界时必须附带具体 makespan 和计划。
		if res.Makespan <= 0 || len(res.Plans) == 0 {
			t.Fatal("feasible=true 时必须给出 makespan 和计划")
		}
	}
	t.Logf("上限2: feasible=%v optimal=%v makespan=%d reason=%q nodes=%d",
		res.Feasible, res.Optimal, res.Makespan, res.Reason, res.NodesSearched)

	// 上限放宽后应求得并证明最优，且与独立穷举器一致。
	full := solver.Solve(p, 0)
	bf, bfOK := bruteforce.MinMakespan(p)
	if !full.Feasible || !full.Optimal {
		t.Fatalf("放宽上限后应得已证最优可行解，实际 feasible=%v optimal=%v", full.Feasible, full.Optimal)
	}
	if !bfOK || full.Makespan != bf {
		t.Fatalf("最优 makespan 应与穷举器一致 = %d，实际 %d", bf, full.Makespan)
	}
}

// TestEmptyTasks：没有任务时立即返回 makespan=0 的空计划(边界)。
func TestEmptyTasks(t *testing.T) {
	p := &model.Problem{
		Depot:    model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots:   []model.Robot{{ID: "r1", Start: model.Point{X: 0, Y: 0}, StartBattery: 5, BatteryCapacity: 5, Capacity: 1, EnergyPerDist: 1, ChargeRate: 1}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 9, Y: 9}}},
	}
	res := solver.Solve(p, 0)
	if !res.Feasible || !res.Optimal || res.Makespan != 0 {
		t.Fatalf("空任务应可行且 makespan=0，实际 %+v", res)
	}
	if len(res.Plans) != 1 || len(res.Plans[0].Actions) != 0 {
		t.Fatalf("空任务应有 1 条空计划，实际 %s", fmt.Sprint(res.Plans))
	}
}
