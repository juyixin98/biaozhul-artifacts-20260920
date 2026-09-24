package solver_test

import (
	"fmt"
	"math/rand"
	"testing"

	"github.com/example/robot-task/internal/bruteforce"
	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/solver"
)

// genInstance 生成满足穷举器前提的小实例：
// 任意相关点间单段距离耗能 <= 电池容量(途中最多充一次即完备)。
//
// lowBattery=true 时所有机器人初始电量很低，迫使计划必须在途中充电，
// 用于覆盖“必须先充电”的场景。
func genInstance(rng *rand.Rand, nRobot, nTask int, lowBattery bool) *model.Problem {
	depot := model.Point{X: 0, Y: 0}
	// 普通场景充电点放在角落；低电量场景充电点放近(2,2)/(-2,-2)，
	// 使“可行但必须先充电”的实例大量出现。
	chargers := []model.Charger{
		{ID: "c1", Location: model.Point{X: 4, Y: 4}},
		{ID: "c2", Location: model.Point{X: -4, Y: -4}},
	}
	span := 5 // [-4,4]
	if lowBattery {
		chargers[0].Location = model.Point{X: 2, Y: 2}
		chargers[1].Location = model.Point{X: -2, Y: -2}
		span = 3 // [-2,2]，起点和任务都靠近 depot/充电点
	}
	p := &model.Problem{Depot: model.Depot{Location: depot}, Chargers: chargers}
	for i := 0; i < nRobot; i++ {
		startBat := int64(6 + rng.Intn(15)) // 6..20
		if lowBattery {
			startBat = int64(1 + rng.Intn(5)) // 1..5：不充电几乎无法完成任务
		}
		p.Robots = append(p.Robots, model.Robot{
			ID: fmt.Sprintf("r%d", i+1),
			Start: model.Point{
				X: int64(rng.Intn(span)) - int64(span/2),
				Y: int64(rng.Intn(span)) - int64(span/2),
			},
			StartBattery:    startBat,
			BatteryCapacity: 20,
			Capacity:        10,
			EnergyPerDist:   1,
			ChargeRate:      int64(1 + rng.Intn(2)),
		})
	}
	for i := 0; i < nTask; i++ {
		ready := int64(rng.Intn(21))
		// 部分时间窗紧迫，制造无解实例；电量受限时放宽窗以便靠充电完成。
		width := 5 + rng.Intn(30)
		if lowBattery {
			width = 20 + rng.Intn(40)
		}
		p.Tasks = append(p.Tasks, model.Task{
			ID: fmt.Sprintf("t%d", i+1),
			Location: model.Point{
				X: int64(rng.Intn(span)) - int64(span/2),
				Y: int64(rng.Intn(span)) - int64(span/2),
			},
			Load:        int64(1 + rng.Intn(5)),
			Ready:       ready,
			Due:         ready + int64(width),
			ServiceTime: int64(rng.Intn(4)),
		})
	}
	return p
}

// TestBruteForceCrossCheck：对大量小实例，求解器的可行性结论与最优 makespan
// 必须与独立穷举器完全一致；可行解同时通过独立校验器(每段电量非负、任务恰好一次)。
func TestBruteForceCrossCheck(t *testing.T) {
	type shape struct {
		robots, tasks, seeds int
		lowBattery           bool
	}
	shapes := []shape{
		{1, 3, 60, false},
		{2, 3, 40, false},
		{2, 4, 30, false},
		{3, 3, 30, false},
		{1, 5, 15, false},
		{1, 3, 60, true}, // 强制充电
		{2, 3, 40, true},
		{2, 4, 20, true},
	}

	var feasibleN, infeasibleN, mustChargeN int
	for _, sh := range shapes {
		for seed := int64(0); seed < int64(sh.seeds); seed++ {
			rng := rand.New(rand.NewSource(seed*1000 + int64(sh.robots*100+sh.tasks)))
			p := genInstance(rng, sh.robots, sh.tasks, sh.lowBattery)

			bf, bfOK := bruteforce.MinMakespan(p)
			res := solver.Solve(p, solver.DefaultNodeLimit)

			if bfOK != res.Feasible {
				t.Fatalf("shape=%+v seed=%d 可行性不一致: 穷举 feasible=%v, 求解器 feasible=%v (%s)",
					sh, seed, bfOK, res.Feasible, res.Reason)
			}
			if bfOK {
				feasibleN++
				assertValid(t, p, res, true)
				if res.Makespan != bf {
					t.Fatalf("shape=%+v seed=%d makespan 不一致: 穷举=%d 求解器=%d",
						sh, seed, bf, res.Makespan)
				}
				if sh.lowBattery && planHasCharge(res) {
					mustChargeN++
				}
			} else {
				infeasibleN++
			}
		}
	}
	t.Logf("交叉核对完成: 可行 %d(其中含充电动作 %d), 无解 %d, makespan 全部一致",
		feasibleN, mustChargeN, infeasibleN)
}

func planHasCharge(res model.Result) bool {
	for _, pl := range res.Plans {
		for _, a := range pl.Actions {
			if a.Type == "charge" {
				return true
			}
		}
	}
	return false
}
