package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/jmoiron/sqlx"
)

// ExperimentService records and queries experiment runs.
type ExperimentService struct {
	DB *sqlx.DB
}

// CreateExperimentInput is the POST /experiments request body.
type CreateExperimentInput struct {
	Name           string          `json:"name"`
	ModelVersionID int64           `json:"model_version_id"`
	DatasetID      int64           `json:"dataset_id"`
	Metrics        json.RawMessage `json:"metrics"`
	Notes          string          `json:"notes"`
}

// Create records an experiment run for a model version on a published
// dataset, both owned by the caller. Metrics must be a JSON object.
func (s *ExperimentService) Create(ctx context.Context, owner string,
	in CreateExperimentInput) (*Experiment, error) {
	if in.Name == "" {
		return nil, BadRequestf("name is required")
	}

	var versionExists bool
	if err := s.DB.GetContext(ctx, &versionExists, `
		SELECT EXISTS(
			SELECT 1 FROM model_versions mv
			JOIN models m ON m.id = mv.model_id
			WHERE mv.id=$1 AND m.owner_id=$2)`,
		in.ModelVersionID, owner); err != nil {
		return nil, err
	}
	if !versionExists {
		return nil, NotFoundf("model version %d not found", in.ModelVersionID)
	}

	var ds Dataset
	err := s.DB.GetContext(ctx, &ds, `SELECT * FROM datasets WHERE id=$1`, in.DatasetID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("dataset %d not found", in.DatasetID)
	}
	if err != nil {
		return nil, err
	}
	if ds.OwnerID != owner {
		return nil, NotFoundf("dataset %d not found", in.DatasetID)
	}
	if ds.Status != DatasetStatusPublished {
		return nil, Conflictf("dataset %d is not published", in.DatasetID)
	}

	metrics := in.Metrics
	if len(metrics) == 0 {
		metrics = json.RawMessage(`{}`)
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(metrics, &obj); err != nil {
		return nil, BadRequestf("metrics must be a JSON object")
	}

	var exp Experiment
	if err := s.DB.GetContext(ctx, &exp, `
		INSERT INTO experiments (owner_id, name, model_version_id, dataset_id, metrics, notes)
		VALUES ($1,$2,$3,$4,$5::jsonb,$6) RETURNING *`,
		owner, in.Name, in.ModelVersionID, in.DatasetID, string(metrics), in.Notes); err != nil {
		return nil, err
	}
	return &exp, nil
}

// ExperimentFilter narrows an experiment listing. Nil fields are ignored.
type ExperimentFilter struct {
	ModelID        *int64
	ModelVersionID *int64
	DatasetID      *int64
}

// List returns the caller's experiments matching the filter, oldest first.
func (s *ExperimentService) List(ctx context.Context, owner string, f ExperimentFilter) ([]Experiment, error) {
	args := []any{owner}
	conds := []string{"e.owner_id=$1"}
	join := ""

	if f.ModelVersionID != nil {
		args = append(args, *f.ModelVersionID)
		conds = append(conds, fmt.Sprintf("e.model_version_id=$%d", len(args)))
	}
	if f.DatasetID != nil {
		args = append(args, *f.DatasetID)
		conds = append(conds, fmt.Sprintf("e.dataset_id=$%d", len(args)))
	}
	if f.ModelID != nil {
		join = " JOIN model_versions mv ON mv.id = e.model_version_id"
		args = append(args, *f.ModelID)
		conds = append(conds, fmt.Sprintf("mv.model_id=$%d", len(args)))
	}

	query := `SELECT e.* FROM experiments e` + join +
		` WHERE ` + strings.Join(conds, " AND ") + ` ORDER BY e.id`
	experiments := []Experiment{}
	err := s.DB.SelectContext(ctx, &experiments, query, args...)
	return experiments, err
}

// Delete removes one of the caller's experiments.
func (s *ExperimentService) Delete(ctx context.Context, owner string, id int64) error {
	res, err := s.DB.ExecContext(ctx,
		`DELETE FROM experiments WHERE id=$1 AND owner_id=$2`, id, owner)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return NotFoundf("experiment %d not found", id)
	}
	return nil
}
