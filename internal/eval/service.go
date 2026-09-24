package eval

import (
	"context"
	"time"

	"github.com/example/rollout/internal/models"
	"github.com/example/rollout/internal/store"
)

// Store is the persistence surface the service needs.
type Store interface {
	CreateRelease(ctx context.Context, r models.Release) error
	GetRelease(ctx context.Context, id string) (models.Release, error)
	ListReleases(ctx context.Context) ([]models.Release, error)
	GetLatestThreshold(ctx context.Context) (models.ThresholdVersion, error)
	GetThreshold(ctx context.Context, version int) (models.ThresholdVersion, error)
	CreateThreshold(ctx context.Context, spec models.ThresholdSpec, description string) (models.ThresholdVersion, error)
	ListEvents(ctx context.Context, releaseID string) ([]models.Event, error)
	ListObservations(ctx context.Context, releaseID string) ([]store.Observation, error)
	InsertObservationDirect(ctx context.Context, o store.Observation) error
	PutIdempotentResponse(ctx context.Context, digest string, resp store.StoredResponse) error
	GetIdempotentResponse(ctx context.Context, digest string) (store.StoredResponse, bool, error)
	ConsumeNonce(ctx context.Context, digest string, expiresAtUnix int64) (bool, error)
	WithTx(ctx context.Context, fn func(tx store.Tx) error) error
}

// Releaser is the orchestrator over the evaluator and the store.
type Releaser struct {
	Store Store
	Eval  *Evaluator
	Now   func() time.Time
}

func NewReleaser(s Store, ev *Evaluator) *Releaser {
	return &Releaser{Store: s, Eval: ev, Now: time.Now}
}

// CreateReleaseInput is the POST /releases request body.
type CreateReleaseInput struct {
	Name          string `json:"name"`
	Version       string `json:"version"`
	Scenario      string `json:"scenario"`
	MetricURL     string `json:"metric_url,omitempty"` // optional override
	ObservationMS int64  `json:"observation_ms,omitempty"`
	MinSamples    int64  `json:"min_samples,omitempty"`
}

type CommandInput struct {
	ExpectedGeneration int64 `json:"expected_generation"`
}

// weightOf maps a stage to its traffic percentage.
func weightOf(s models.Stage) float64 {
	switch s {
	case models.Stage5:
		return 0.05
	case models.Stage20:
		return 0.20
	case models.Stage50:
		return 0.50
	default:
		return 1.0
	}
}

// alignWindow snaps a stage-entry timestamp to the metrics tick grid so the
// observation window tiles whole buckets (observation_ms is always a whole
// multiple of the 200ms tick).
func alignWindow(t time.Time) time.Time {
	return t.Truncate(200 * time.Millisecond)
}
