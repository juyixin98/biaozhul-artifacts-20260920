package service

import (
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"sircc/internal/domain"
	"sircc/internal/store"
)

// uuidFromPg converts a nullable pgtype.UUID to a *string.
func uuidFromPg(v pgtype.UUID) *string {
	if !v.Valid {
		return nil
	}
	id := uuid.UUID(v.Bytes)
	s := id.String()
	return &s
}

type phaseTimes map[string]pgtype.Timestamptz

func phasesToTimes(rows []store.ListPhasesWithActorRow) phaseTimes {
	out := phaseTimes{}
	for _, r := range rows {
		out[r.Phase] = r.EnteredAt
	}
	return out
}

func secondsBetween(from, to pgtype.Timestamptz) *float64 {
	if !from.Valid || !to.Valid {
		return nil
	}
	d := to.Time.UTC().Sub(from.Time.UTC()).Seconds()
	if d < 0 {
		d = 0
	}
	return &d
}

// computeMetrics derives every duration solely from recorded phase entry
// timestamps. When the phase that ends an interval has not been entered,
// the value is null rather than computed against the current wall clock.
func computeMetrics(p phaseTimes) *PhaseMetrics {
	m := &PhaseMetrics{PhaseDurationsSeconds: map[string]float64{}}

	order := []string{
		domain.PhaseDetection,
		domain.PhaseTriage,
		domain.PhaseContainment,
		domain.PhaseEradication,
		domain.PhaseRecovery,
		domain.PhaseReview,
		domain.PhaseClosure,
	}
	// Per-phase durations: entry of phase N until entry of phase N+1.
	// The last entered phase is still open and has no fabricated end.
	for i := 0; i+1 < len(order); i++ {
		cur, next := order[i], order[i+1]
		if d := secondsBetween(p[cur], p[next]); d != nil {
			m.PhaseDurationsSeconds[cur] = *d
		}
	}

	// Containment: detection entered -> containment entered.
	m.TimeToContainmentSeconds = secondsBetween(p[domain.PhaseDetection], p[domain.PhaseContainment])
	// Time spent inside the containment phase itself.
	m.ContainmentPhaseSeconds = secondsBetween(p[domain.PhaseContainment], p[domain.PhaseEradication])
	// Resolution: detection -> recovery entered.
	m.TimeToResolutionSeconds = secondsBetween(p[domain.PhaseDetection], p[domain.PhaseRecovery])
	// Closure: detection -> closure entered.
	m.TimeToClosureSeconds = secondsBetween(p[domain.PhaseDetection], p[domain.PhaseClosure])

	return m
}
