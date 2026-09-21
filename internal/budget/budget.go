// Package budget evaluates per-URL/per-viewport performance budgets against
// successful runs. Missing (unsupported/failed) metrics skip the evaluation
// rather than being treated as zero or as pass.
package budget

import (
	"context"

	"sitevitals/internal/collector"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

// Evaluator evaluates matching budgets for a finished successful run.
type Evaluator struct{ Repo *store.Repo }

// NewEvaluator constructs an Evaluator.
func NewEvaluator(repo *store.Repo) *Evaluator { return &Evaluator{Repo: repo} }

// Evaluate loads matching budgets, compares them with collected metrics and
// persists one BudgetEvaluation per budget. Failed runs never reach this path,
// so they can never contaminate threshold statistics.
func (e *Evaluator) Evaluate(ctx context.Context, run *models.Run) ([]models.BudgetEvaluation, error) {
	budgets, err := e.Repo.MatchingBudgets(ctx, run.TargetURL, run.Viewport)
	if err != nil {
		return nil, err
	}
	art, err := e.Repo.GetRunArtifacts(ctx, run.ID)
	if err != nil {
		return nil, err
	}
	byName := map[string]*models.Metric{}
	for i := range art.Metrics {
		byName[art.Metrics[i].Name] = &art.Metrics[i]
	}
	out := make([]models.BudgetEvaluation, 0, len(budgets))
	for i := range budgets {
		b := budgets[i]
		ev := models.BudgetEvaluation{
			BudgetID: b.ID, RunID: run.ID, JobID: run.JobID,
			TargetURL: run.TargetURL, Viewport: run.Viewport, Metric: b.Metric,
			ThresholdMS: b.ThresholdMS, ThresholdCLS: b.ThresholdCLS,
		}
		m := byName[b.Metric]
		// No collected value -> skip; never compare against an implicit zero.
		if m == nil || m.Status != models.MetricCollected {
			ev.Skipped = true
			if err := e.Repo.CreateBudgetEvaluation(ctx, &ev); err != nil {
				return nil, err
			}
			out = append(out, ev)
			continue
		}
		switch b.Metric {
		case collector.MetricCLS:
			if m.ValueCLS != nil {
				ev.ActualCLS = m.ValueCLS
				if b.ThresholdCLS != nil {
					ev.Exceeded = *m.ValueCLS > *b.ThresholdCLS
				}
			} else {
				ev.Skipped = true
			}
		default:
			if m.ValueMS != nil {
				ev.ActualMS = m.ValueMS
				if b.ThresholdMS != nil {
					ev.Exceeded = *m.ValueMS > *b.ThresholdMS
				}
			} else {
				ev.Skipped = true
			}
		}
		if err := e.Repo.CreateBudgetEvaluation(ctx, &ev); err != nil {
			return nil, err
		}
		out = append(out, ev)
	}
	return out, nil
}
