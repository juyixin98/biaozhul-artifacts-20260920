// Package experiments records inference runs and compares model versions.
package experiments

import (
	"context"
	"database/sql"
	"errors"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/models"
)

var (
	ErrNotFound          = errors.New("model version not found")
	ErrClassTablesDiffer = errors.New("class tables differ: metrics are not comparable")
)

type Service struct{ db *sqlx.DB }

func NewService(db *sqlx.DB) *Service { return &Service{db: db} }

// Record stores one inference run.
func (s *Service) Record(ctx context.Context, r *models.ExperimentRecord) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO experiment_records
		   (model_version_id, caller_id, input_digest, input_dim,
		    predicted_class, predicted_index, confidence, latency_ms)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		r.ModelVersionID, r.CallerID, r.InputDigest, r.InputDim,
		r.PredictedClass, r.PredictedIndex, r.Confidence, r.LatencyMs)
	return err
}

// List returns records for a model version the caller owns.
func (s *Service) List(ctx context.Context, callerID, modelVersionID int64, limit, offset int) ([]models.ExperimentRecord, error) {
	var owner int64
	err := s.db.GetContext(ctx, &owner, `SELECT owner_id FROM model_versions WHERE id = $1`, modelVersionID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if owner != callerID {
		return nil, ErrNotFound
	}
	var out []models.ExperimentRecord
	err = s.db.SelectContext(ctx, &out,
		`SELECT id, model_version_id, caller_id, input_digest, input_dim,
		        predicted_class, predicted_index, confidence, latency_ms, created_at
		 FROM experiment_records WHERE model_version_id = $1
		 ORDER BY id DESC LIMIT $2 OFFSET $3`,
		modelVersionID, limit, offset)
	return out, err
}

// Metrics are per-version aggregates over experiment_records.
type Metrics struct {
	ModelVersionID int64          `json:"model_version_id"`
	TotalRuns      int            `json:"total_runs"`
	AvgLatencyMs   float64        `json:"avg_latency_ms"`
	AvgConfidence  float64        `json:"avg_confidence"`
	ClassCounts    map[string]int `json:"class_counts"`
}

type Comparison struct {
	ClassTableDigest string   `json:"class_table_digest"`
	Classes          []string `json:"classes"`
	A                Metrics  `json:"a"`
	B                Metrics  `json:"b"`
}

// Compare aggregates two versions, but refuses when their class tables differ:
// per-class prediction counts and confidences are meaningless across different
// label sets, even if their sizes happen to match.
func (s *Service) Compare(ctx context.Context, callerID, idA, idB int64) (*Comparison, error) {
	type ver struct {
		ID               int64              `db:"id"`
		OwnerID          int64              `db:"owner_id"`
		ClassTableDigest string             `db:"class_table_digest"`
		Classes          models.StringArray `db:"classes"`
	}
	var a, b ver
	for _, pair := range []struct {
		id  int64
		dst *ver
	}{{idA, &a}, {idB, &b}} {
		err := s.db.GetContext(ctx, pair.dst,
			`SELECT id, owner_id, class_table_digest, classes
			 FROM model_versions WHERE id = $1`, pair.id)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		if err != nil {
			return nil, err
		}
		if pair.dst.OwnerID != callerID {
			return nil, ErrNotFound
		}
	}
	if a.ClassTableDigest != b.ClassTableDigest {
		return nil, ErrClassTablesDiffer
	}

	ma, err := s.metrics(ctx, a.ID, a.Classes)
	if err != nil {
		return nil, err
	}
	mb, err := s.metrics(ctx, b.ID, b.Classes)
	if err != nil {
		return nil, err
	}
	return &Comparison{
		ClassTableDigest: a.ClassTableDigest,
		Classes:          []string(a.Classes),
		A:                ma,
		B:                mb,
	}, nil
}

func (s *Service) metrics(ctx context.Context, versionID int64, classes []string) (Metrics, error) {
	m := Metrics{ModelVersionID: versionID, ClassCounts: map[string]int{}}
	for _, c := range classes {
		m.ClassCounts[c] = 0
	}
	row := s.db.QueryRowxContext(ctx,
		`SELECT count(*), COALESCE(avg(latency_ms),0), COALESCE(avg(confidence),0)
		 FROM experiment_records WHERE model_version_id = $1`, versionID)
	if err := row.Scan(&m.TotalRuns, &m.AvgLatencyMs, &m.AvgConfidence); err != nil {
		return m, err
	}
	rows, err := s.db.QueryxContext(ctx,
		`SELECT predicted_class, count(*) FROM experiment_records
		 WHERE model_version_id = $1 GROUP BY predicted_class`, versionID)
	if err != nil {
		return m, err
	}
	defer rows.Close()
	for rows.Next() {
		var class string
		var n int
		if err := rows.Scan(&class, &n); err != nil {
			return m, err
		}
		m.ClassCounts[class] = n
	}
	return m, rows.Err()
}
