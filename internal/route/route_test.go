package route_test

import (
	"testing"

	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/route"
)

// TestParetoDominance：直达 t=4/电15 在(时间,电量)上支配充满路线
// t=26/电12(更早且电量更高)，因此被支配路线必须被剪掉。
// 互不支配方案的保留由 TestTwoNonDominated 覆盖。
func TestParetoDominance(t *testing.T) {
	from := model.Point{X: 2, Y: -2}
	target := model.Point{X: 0, Y: 0}
	chargers := []model.Charger{{ID: "c1", Location: model.Point{X: 4, Y: 4}}}
	opts := route.ReachOptions(from, 0, 19, target, 20, 1, 2, 1<<62, 0, chargers)
	if len(opts) != 1 {
		t.Fatalf("无电量下限时应只剩不被支配的直达方案，实际 %+v", opts)
	}
	if opts[0].ArrivalTime != 4 || opts[0].ArrivalBattery != 15 {
		t.Fatalf("保留方案应为 t=4/电15，实际 t=%d/电=%d", opts[0].ArrivalTime, opts[0].ArrivalBattery)
	}
}

// TestTwoNonDominated：直达(早到、电量0)与中途充满(晚到、电量4)
// 互不支配：前者时间优、后者电量优，二者都必须保留。
func TestTwoNonDominated(t *testing.T) {
	// (0,0) -> (6,0)，充电点 (3,0)，容量10，初始6，耗能1/格，充电1/时间。
	from := model.Point{X: 0, Y: 0}
	target := model.Point{X: 6, Y: 0}
	chargers := []model.Charger{{ID: "c1", Location: model.Point{X: 3, Y: 0}}}
	opts := route.ReachOptions(from, 0, 6, target, 10, 1, 1, 1<<62, 0, chargers)
	if len(opts) != 2 {
		t.Fatalf("期望 2 个互不支配方案，实际 %d: %+v", len(opts), opts)
	}
	got := map[[2]int64]bool{}
	for _, o := range opts {
		got[[2]int64{o.ArrivalTime, o.ArrivalBattery}] = true
	}
	// 直达：t=6, bat=0；充电：t=3+7+3=13, bat=10-3=7。
	if !got[[2]int64{6, 0}] || !got[[2]int64{13, 7}] {
		t.Fatalf("缺少 (6,0) 或 (13,7) 方案: %v", got)
	}
}

// TestBatteryCutoff：初始电量连最近充电点都到不了，只剩可行直达。
func TestBatteryCutoff(t *testing.T) {
	from := model.Point{X: 0, Y: 0}
	target := model.Point{X: 3, Y: 0}
	chargers := []model.Charger{{ID: "c1", Location: model.Point{X: 10, Y: 0}}}
	opts := route.ReachOptions(from, 0, 3, target, 20, 1, 1, 100, 0, chargers)
	if len(opts) != 1 || opts[0].ArrivalTime != 3 {
		t.Fatalf("期望仅 1 个直达方案，实际 %+v", opts)
	}
}

// TestDeadlinePrune：截止时刻早于任何到达，无方案。
func TestDeadlinePrune(t *testing.T) {
	from := model.Point{X: 0, Y: 0}
	target := model.Point{X: 10, Y: 0}
	chargers := []model.Charger{{ID: "c1", Location: model.Point{X: 5, Y: 0}}}
	opts := route.ReachOptions(from, 0, 20, target, 20, 1, 1, 9, 0, chargers)
	if len(opts) != 0 {
		t.Fatalf("deadline=9 时不应有方案，实际 %+v", opts)
	}
}

// TestTwoChargersMultiHop：电池极小，必须依次在两个充电点充电才能到目标。
func TestTwoChargersMultiHop(t *testing.T) {
	// 0 -> c1(3,0) 距离3 -> c2(6,0) 距离3 -> target(9,0) 距离3。
	from := model.Point{X: 0, Y: 0}
	target := model.Point{X: 9, Y: 0}
	chargers := []model.Charger{
		{ID: "c1", Location: model.Point{X: 3, Y: 0}},
		{ID: "c2", Location: model.Point{X: 6, Y: 0}},
	}
	// 容量 5：单段 3 耗能可行，但直达 9 耗能不可行；初始电量 5 需在 c1、c2 各充满。
	opts := route.ReachOptions(from, 0, 5, target, 5, 1, 0, 100, 0, chargers)
	if len(opts) != 1 {
		t.Fatalf("容量5、容量恰够单段时应只有 1 条两充方案，实际 %d: %+v", len(opts), opts)
	}
	o := opts[0]
	var charged []string
	for _, h := range o.Path {
		if h.Charged {
			charged = append(charged, h.ChargerID)
		}
	}
	if len(charged) != 2 || charged[0] != "c1" || charged[1] != "c2" {
		t.Fatalf("期望依次在 c1,c2 充满，实际 %v", charged)
	}
}
