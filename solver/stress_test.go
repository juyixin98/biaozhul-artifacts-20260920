//go:build stress

// Run with: go test -tags stress ./solver/ -run TestStressLargest -v
// This exercises the size cap (10 tasks, 2 robots, 2 chargers) and
// cross-checks against the independent brute force; it takes a few
// seconds and is excluded from the default test run.

package solver_test

import (
	"fmt"
	"testing"
	"time"

	"robotdispatch/model"
)

func TestStressLargest(t *testing.T) {
	req := &model.Request{}
	for i := 0; i < 2; i++ {
		req.Robots = append(req.Robots, model.Robot{
			ID: fmt.Sprintf("r%d", i+1), Start: p(0, 0),
			Battery: 30, BatteryCapacity: 30, ChargeRate: 5, PayloadCapacity: 5,
		})
	}
	for i := 0; i < 10; i++ {
		req.Tasks = append(req.Tasks, model.Task{
			ID:      fmt.Sprintf("j%d", i+1),
			Loc:     p(int64(2+(i*3)%12), int64((i*5)%10)),
			Payload: int64(1 + i%3), Ready: int64(i), Due: int64(40 + i*2), Service: 1,
		})
	}
	req.Chargers = []model.Charger{
		{ID: "c1", Loc: p(3, 3)}, {ID: "c2", Loc: p(9, 6)},
	}
	epd, _ := req.Validate()
	start := time.Now()
	resp := solveReq(req)
	elapsed := time.Since(start)
	ok, opt := refOptimal(req, epd)
	t.Logf("feasible=%v makespan=%d bruteForce=%d time=%s", resp.Feasible, resp.Makespan, opt, elapsed)
	if ok {
		verifyPlan(t, req, resp, opt)
	} else if resp.Feasible {
		t.Fatal("solver feasible but brute force infeasible")
	}
	if elapsed > 5*time.Second {
		t.Fatalf("too slow: %s", elapsed)
	}
}
