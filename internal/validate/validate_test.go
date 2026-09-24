package validate_test

import (
	"strings"
	"testing"

	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/validate"
)

func baseProblem() *model.Problem {
	return &model.Problem{
		Depot:    model.Depot{Location: model.Point{X: 0, Y: 0}},
		Robots:   []model.Robot{{ID: "r1", Start: model.Point{X: 0, Y: 0}, StartBattery: 20, BatteryCapacity: 20, Capacity: 10, EnergyPerDist: 1, ChargeRate: 1}},
		Chargers: []model.Charger{{ID: "c1", Location: model.Point{X: 0, Y: 5}}},
		Tasks: []model.Task{{
			ID: "t1", Location: model.Point{X: 3, Y: 0},
			Load: 1, Ready: 0, Due: 100, ServiceTime: 1,
		}},
	}
}

// 一份物理正确的计划：depot(0,0) 取货 -> 行驶到 (3,0) -> 服务。
func validPlan() []model.RobotPlan {
	return []model.RobotPlan{{
		RobotID: "r1",
		Actions: []model.Action{
			{RobotID: "r1", Type: "pickup", TaskID: "t1", From: model.Point{X: 0, Y: 0}, To: model.Point{X: 0, Y: 0}, StartTime: 0, ArrivalTime: 0, EndTime: 0, BatteryBefore: 20, BatteryAfter: 20},
			{RobotID: "r1", Type: "travel", From: model.Point{X: 0, Y: 0}, To: model.Point{X: 3, Y: 0}, StartTime: 0, TravelTime: 3, ArrivalTime: 3, EndTime: 3, BatteryBefore: 20, BatteryAfter: 17},
			{RobotID: "r1", Type: "service", TaskID: "t1", From: model.Point{X: 3, Y: 0}, To: model.Point{X: 3, Y: 0}, StartTime: 3, ArrivalTime: 3, ServiceTime: 1, EndTime: 4, BatteryBefore: 17, BatteryAfter: 17},
		},
		Makespan: 4,
	}}
}

func TestValidPlan(t *testing.T) {
	rep := validate.Check(baseProblem(), validPlan())
	if !rep.Valid {
		t.Fatalf("正确计划应通过校验，实际错误: %v", rep.Errors)
	}
	if rep.Makespan != 4 {
		t.Fatalf("makespan 应为 4，实际 %d", rep.Makespan)
	}
}

// TestNegativeBatteryRejected：行驶后电量为负必须报错。
func TestNegativeBatteryRejected(t *testing.T) {
	p := baseProblem()
	plans := validPlan()
	// 篡改行驶段，让耗电超过电量。
	plans[0].Actions[1].BatteryBefore = 2
	plans[0].Actions[1].BatteryAfter = -1
	plans[0].Actions[2].BatteryBefore = -1
	plans[0].Actions[2].BatteryAfter = -1
	rep := validate.Check(p, plans)
	if rep.Valid || !containsAny(rep.Errors, "为负", "超过出发前电量") {
		t.Fatalf("应捕获负电量，实际 valid=%v errors=%v", rep.Valid, rep.Errors)
	}
}

// TestMissingServiceRejected：任务取了货却没服务 -> 必须报“恰好一次”。
func TestMissingServiceRejected(t *testing.T) {
	p := baseProblem()
	plans := validPlan()
	plans[0].Actions = plans[0].Actions[:2] // 去掉 service
	plans[0].Makespan = 3
	rep := validate.Check(p, plans)
	if rep.Valid || !containsAny(rep.Errors, "被服务 0 次") {
		t.Fatalf("应捕获任务未被服务，实际 %v", rep.Errors)
	}
}

// TestServiceTwiceRejected：同一任务服务两次必须报错。
func TestServiceTwiceRejected(t *testing.T) {
	p := baseProblem()
	plans := validPlan()
	extra := plans[0].Actions[2]
	plans[0].Actions = append(plans[0].Actions, extra)
	rep := validate.Check(p, plans)
	if rep.Valid || !containsAny(rep.Errors, "被服务 2 次") {
		t.Fatalf("应捕获重复服务，实际 %v", rep.Errors)
	}
}

// TestOutOfWindowRejected：服务开始时间超出 due 必须报错。
func TestOutOfWindowRejected(t *testing.T) {
	p := baseProblem()
	plans := validPlan()
	plans[0].Actions[2].EndTime = 200 // 服务在 199 开始，超过 due=100
	plans[0].Makespan = 200
	rep := validate.Check(p, plans)
	if rep.Valid || !containsAny(rep.Errors, "超出时间窗") {
		t.Fatalf("应捕获超出时间窗，实际 %v", rep.Errors)
	}
}

// TestOverCapacityRejected：取货后载荷超过容量必须报错。
func TestOverCapacityRejected(t *testing.T) {
	p := baseProblem()
	p.Tasks[0].Load = 50 // 超过容量 10
	rep := validate.Check(p, validPlan())
	if rep.Valid || !containsAny(rep.Errors, "超过容量") {
		t.Fatalf("应捕获超载，实际 %v", rep.Errors)
	}
}

// TestChargeActionChecked：合法充电动作通过，非法充电量报错。
func TestChargeActionChecked(t *testing.T) {
	p := baseProblem()
	plans := []model.RobotPlan{{
		RobotID: "r1",
		Actions: []model.Action{
			// 行驶 5 格到充电点 (0,5)：20->15
			{RobotID: "r1", Type: "charge", ChargerID: "c1", From: model.Point{X: 0, Y: 0}, To: model.Point{X: 0, Y: 5}, StartTime: 0, TravelTime: 5, ArrivalTime: 5, ServiceTime: 5, EndTime: 10, BatteryBefore: 20, BatteryAfter: 20},
			// 开回 depot (0,0)：20->15
			{RobotID: "r1", Type: "travel", From: model.Point{X: 0, Y: 5}, To: model.Point{X: 0, Y: 0}, StartTime: 10, TravelTime: 5, ArrivalTime: 15, EndTime: 15, BatteryBefore: 20, BatteryAfter: 15},
			{RobotID: "r1", Type: "pickup", TaskID: "t1", From: model.Point{X: 0, Y: 0}, To: model.Point{X: 0, Y: 0}, StartTime: 15, ArrivalTime: 15, EndTime: 15, BatteryBefore: 15, BatteryAfter: 15},
			{RobotID: "r1", Type: "travel", From: model.Point{X: 0, Y: 0}, To: model.Point{X: 3, Y: 0}, StartTime: 15, TravelTime: 3, ArrivalTime: 18, EndTime: 18, BatteryBefore: 15, BatteryAfter: 12},
			{RobotID: "r1", Type: "service", TaskID: "t1", From: model.Point{X: 3, Y: 0}, To: model.Point{X: 3, Y: 0}, StartTime: 18, ArrivalTime: 18, ServiceTime: 1, EndTime: 19, BatteryBefore: 12, BatteryAfter: 12},
		},
		Makespan: 19,
	}}
	rep := validate.Check(p, plans)
	if !rep.Valid {
		t.Fatalf("含合法充电的计划应通过，实际 %v", rep.Errors)
	}

	// 篡改充电耗时：充 5 电按 rate=1 应为 5，写成 9 必须报错。
	plans[0].Actions[0].ServiceTime = 9
	plans[0].Actions[0].EndTime = 14
	rep = validate.Check(p, plans)
	if rep.Valid || !containsAny(rep.Errors, "service_time") {
		t.Fatalf("应捕获充电耗时不符，实际 %v", rep.Errors)
	}
}

func containsAny(errs []string, subs ...string) bool {
	for _, e := range errs {
		for _, s := range subs {
			if strings.Contains(e, s) {
				return true
			}
		}
	}
	return false
}
