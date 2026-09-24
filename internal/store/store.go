// Package store defines persistence interfaces and provides a PostgreSQL
// implementation and an in-memory one for tests.
package store

import (
	"context"
	"errors"

	"github.com/example/rollout/internal/models"
)

// ErrNotFound is returned when a row does not exist.
var ErrNotFound = errors.New("not found")

// ErrConflict is returned on a generation/version optimistic-lock mismatch.
var ErrConflict = errors.New("conflict: expected generation/version does not match")

// Observation is one persisted evaluation result.
type Observation struct {
	ID          int64
	ReleaseID   string
	Stage       models.Stage
	Generation  int64
	WindowStart string
	WindowEnd   string
	Superseded  bool
	Verdict     models.Verdict
	CreatedAt   string
}

// StoredResponse is a cached idempotent response.
type StoredResponse struct {
	Status int
	Body   []byte
}

// Tx is one atomic unit of work exposed to the service layer.
type Tx interface {
	GetReleaseForUpdate(ctx context.Context, id string) (models.Release, error)
	UpdateRelease(ctx context.Context, r models.Release) error
	InsertEvent(ctx context.Context, e models.Event) error
	InsertObservation(ctx context.Context, o Observation) error
}

// Store is the full persistence surface.
type Store interface {
	// Schema/migrations.
	Migrate(ctx context.Context) error
	Ping(ctx context.Context) error

	// Thresholds.
	GetLatestThreshold(ctx context.Context) (models.ThresholdVersion, error)
	GetThreshold(ctx context.Context, version int) (models.ThresholdVersion, error)
	CreateThreshold(ctx context.Context, spec models.ThresholdSpec, description string) (models.ThresholdVersion, error)

	// Releases.
	CreateRelease(ctx context.Context, r models.Release) error
	GetRelease(ctx context.Context, id string) (models.Release, error)
	ListReleases(ctx context.Context) ([]models.Release, error)

	// Events & observations.
	ListEvents(ctx context.Context, releaseID string) ([]models.Event, error)
	ListObservations(ctx context.Context, releaseID string) ([]Observation, error)
	InsertObservationDirect(ctx context.Context, o Observation) error

	// Anti-replay (real one-time nonces).
	ConsumeNonce(ctx context.Context, digest string, expiresAtUnix int64) (bool, error)

	// Idempotency.
	GetIdempotentResponse(ctx context.Context, digest string) (StoredResponse, bool, error)
	PutIdempotentResponse(ctx context.Context, digest string, resp StoredResponse) error

	// Transactional command application.
	WithTx(ctx context.Context, fn func(tx Tx) error) error
}
