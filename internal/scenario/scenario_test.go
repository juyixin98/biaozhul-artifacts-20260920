package scenario

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/example/retrybudget/internal/clock"
	"github.com/example/retrybudget/internal/fakesvc"
)

// TestRunAll executes every acceptance scenario and requires all checks to
// pass.
func TestRunAll(t *testing.T) {
	for _, res := range RunAll() {
		if !res.Passed {
			for _, c := range res.Checks {
				if !c.Passed {
					t.Errorf("scenario %s: check %s failed: %s", res.Scenario, c.Name, c.Detail)
				}
			}
		}
	}
}

// TestThreeLayerBudgetBound sweeps root budgets under a persistent downstream
// failure: the total number of attempts across all three layers must never
// exceed the root budget, and the outcome must be budget exhaustion.
func TestThreeLayerBudgetBound(t *testing.T) {
	for budget := int64(1); budget <= 6; budget++ {
		t.Run(fmt.Sprintf("budget_%d", budget), func(t *testing.T) {
			fx := newFixture(clock.Real{}, budget)
			res := fx.call(context.Background(), budget, true, time.Time{}, map[string]string{
				fakesvc.HeaderFaultID:        fmt.Sprintf("sweep-%d", budget),
				fakesvc.HeaderFaultFailTimes: "1000",
			})
			if res.TotalAttempts != int(budget) {
				t.Errorf("total attempts = %d, want exactly budget %d", res.TotalAttempts, budget)
			}
			if res.TotalAttempts > int(budget) {
				t.Errorf("root budget violated: %d > %d", res.TotalAttempts, budget)
			}
		})
	}
}
