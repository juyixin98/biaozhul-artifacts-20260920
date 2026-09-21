// Package modelreg implements immutable model version registration, real CPU
// inference with the single supported linear-softmax network, experiment
// logging and version comparison.
package modelreg

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	sq "github.com/Masterminds/squirrel"
	"github.com/jmoiron/sqlx"
	"github.com/lib/pq"

	"github.com/synapticgo/synapticgo/internal/dataset"
	"github.com/synapticgo/synapticgo/internal/httpx"
	"github.com/synapticgo/synapticgo/internal/nn"
	"github.com/synapticgo/synapticgo/internal/spec"
)

// Service exposes model registration, inference and comparison.
type Service struct {
	DB       *sqlx.DB
	Datasets *dataset.Service
}

// RegisterRequest is the immutable snapshot submitted when a version is born.
type RegisterRequest struct {
	Name           string          `json:"name"`
	Version        string          `json:"version"`
	Weights        json.RawMessage `json:"weights"`
	TrainDatasetID *int64          `json:"train_dataset_id,omitempty"`
	EvalDatasetID  *int64          `json:"eval_dataset_id,omitempty"`
	Metrics        spec.Metrics    `json:"metrics,omitempty"`
}

// View is a model version as returned by the API.
type View struct {
	ID              int64           `json:"id" db:"id"`
	OwnerID         int64           `json:"owner_id" db:"owner_id"`
	Name            string          `json:"name" db:"name"`
	Version         string          `json:"version" db:"version"`
	InputDim        int             `json:"input_dim" db:"input_dim"`
	Classes         json.RawMessage `json:"classes" db:"classes"`
	ClassesHash     string          `json:"classes_hash" db:"classes_hash"`
	Weights         json.RawMessage `json:"weights" db:"weights"`
	WeightSHA256    string          `json:"weight_sha256" db:"weight_sha256"`
	TrainDatasetSHA *string         `json:"train_dataset_sha,omitempty" db:"train_dataset_sha"`
	EvalDatasetSHA  *string         `json:"eval_dataset_sha,omitempty" db:"eval_dataset_sha"`
	Metrics         json.RawMessage `json:"metrics" db:"metrics"`
	CreatedAt       time.Time       `json:"created_at" db:"created_at"`
}

type modelRow struct {
	ID              int64           `db:"id"`
	OwnerID         int64           `db:"owner_id"`
	Name            string          `db:"name"`
	Version         string          `db:"version"`
	InputDim        int             `db:"input_dim"`
	Classes         json.RawMessage `db:"classes"`
	ClassesHash     string          `db:"classes_hash"`
	Weights         json.RawMessage `db:"weights"`
	WeightSHA256    string          `db:"weight_sha256"`
	TrainDatasetSHA *string         `db:"train_dataset_sha"`
	EvalDatasetSHA  *string         `db:"eval_dataset_sha"`
	Metrics         json.RawMessage `db:"metrics"`
	CreatedAt       time.Time       `db:"created_at"`
}

var psql = sq.StatementBuilder.PlaceholderFormat(sq.Dollar)

// Register validates the weights, optionally binds ready datasets, and
// stores an immutable version.
func (s *Service) Register(ctx context.Context, ownerID int64, req *RegisterRequest) (*View, error) {
	if len(req.Name) < 1 || len(req.Name) > 200 {
		return nil, httpx.ErrBadRequest("name must be 1..200 characters")
	}
	if len(req.Version) < 1 || len(req.Version) > 64 {
		return nil, httpx.ErrBadRequest("version must be 1..64 characters")
	}
	weights, err := spec.ParseWeights(req.Weights)
	if err != nil {
		return nil, httpx.ErrUnprocess(err.Error())
	}
	canonical, weightSHA, err := spec.CanonicalWeights(weights)
	if err != nil {
		return nil, httpx.ErrUnprocess(err.Error())
	}
	classesJSON, _ := json.Marshal(weights.Classes)
	classesHash := spec.ClassesHash(weights.Classes)

	if err := validateMetrics(req.Metrics); err != nil {
		return nil, httpx.ErrBadRequest(err.Error())
	}
	metricsJSON, err := json.Marshal(orEmptyObject(req.Metrics))
	if err != nil {
		return nil, err
	}

	var trainSHA, evalSHA *string
	if req.TrainDatasetID != nil {
		sha, err := s.readyDatasetSHA(ctx, ownerID, *req.TrainDatasetID, weights, "train")
		if err != nil {
			return nil, err
		}
		trainSHA = &sha
	}
	if req.EvalDatasetID != nil {
		sha, err := s.readyDatasetSHA(ctx, ownerID, *req.EvalDatasetID, weights, "eval")
		if err != nil {
			return nil, err
		}
		evalSHA = &sha
	}

	q, args, err := psql.Insert("model_versions").
		Columns("owner_id", "name", "version", "input_dim", "classes",
			"classes_hash", "weights", "weight_sha256",
			"train_dataset_sha", "eval_dataset_sha", "metrics").
		Values(ownerID, req.Name, req.Version, weights.Dim, classesJSON,
			classesHash, canonical, weightSHA, trainSHA, evalSHA, metricsJSON).
		Suffix("RETURNING id").ToSql()
	if err != nil {
		return nil, err
	}
	var id int64
	if err := s.DB.GetContext(ctx, &id, q, args...); err != nil {
		var pqErr *pq.Error
		if errors.As(err, &pqErr) && pqErr.Code == "23505" {
			return nil, httpx.ErrConflict("model name+version already exists")
		}
		return nil, err
	}
	return s.getOwned(ctx, ownerID, id)
}

// readyDatasetSHA resolves an owned, published dataset and checks that its
// content is structurally compatible with the model (dimensions and labels).
func (s *Service) readyDatasetSHA(ctx context.Context, ownerID, datasetID int64, w *spec.Weights, kind string) (string, error) {
	sha, err := s.Datasets.ReadySHA(ctx, ownerID, datasetID)
	if err != nil {
		return "", err
	}
	raw, _, err := s.Datasets.OpenContent(ctx, ownerID, datasetID)
	if err != nil {
		return "", err
	}
	ds, err := spec.ParseDataset(raw)
	if err != nil {
		return "", httpx.ErrUnprocess(fmt.Sprintf("%s dataset is not a valid dataset file: %v", kind, err))
	}
	if ds.Dim != w.Dim {
		return "", httpx.ErrUnprocess(fmt.Sprintf(
			"%s dataset dim %d does not match model input dim %d", kind, ds.Dim, w.Dim))
	}
	for i, ex := range ds.Examples {
		if ex.Label != nil && *ex.Label >= len(w.Classes) {
			return "", httpx.ErrUnprocess(fmt.Sprintf(
				"%s dataset example %d label %d is outside the class table of size %d",
				kind, i, *ex.Label, len(w.Classes)))
		}
	}
	return sha, nil
}

func validateMetrics(m spec.Metrics) error {
	for k, v := range m {
		f, ok := v.(float64)
		if !ok {
			return fmt.Errorf("metric %q must be a JSON number", k)
		}
		if f != f || f > 1e300 || f < -1e300 { // NaN / absurd magnitudes
			return fmt.Errorf("metric %q is not a finite number", k)
		}
	}
	return nil
}

func orEmptyObject(m spec.Metrics) spec.Metrics {
	if m == nil {
		return spec.Metrics{}
	}
	return m
}

// Get returns one owned model version.
func (s *Service) Get(ctx context.Context, ownerID, id int64) (*View, error) {
	return s.getOwned(ctx, ownerID, id)
}

func (s *Service) getOwned(ctx context.Context, ownerID, id int64) (*View, error) {
	var r modelRow
	err := s.DB.GetContext(ctx, &r, `
		SELECT id, owner_id, name, version, input_dim, classes, classes_hash,
		       weights, weight_sha256, train_dataset_sha, eval_dataset_sha,
		       metrics, created_at
		FROM model_versions WHERE id = $1 AND owner_id = $2`, id, ownerID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, httpx.ErrNotFound("model version not found")
		}
		return nil, err
	}
	return rowToView(&r), nil
}

// List returns the caller's model versions, optionally filtered by name.
func (s *Service) List(ctx context.Context, ownerID int64, name string, limit int) ([]View, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	b := psql.Select(`id, owner_id, name, version, input_dim, classes, classes_hash,
		       weights, weight_sha256, train_dataset_sha, eval_dataset_sha,
		       metrics, created_at`).
		From("model_versions").Where("owner_id = ?", ownerID).
		OrderBy("id DESC").Limit(uint64(limit))
	if name != "" {
		b = b.Where("name = ?", name)
	}
	q, args, err := b.ToSql()
	if err != nil {
		return nil, err
	}
	var rows []modelRow
	if err := s.DB.SelectContext(ctx, &rows, q, args...); err != nil {
		return nil, err
	}
	out := make([]View, len(rows))
	for i := range rows {
		out[i] = *rowToView(&rows[i])
	}
	return out, nil
}

// Delete removes a model version (its experiment rows cascade).
func (s *Service) Delete(ctx context.Context, ownerID, id int64) error {
	res, err := s.DB.ExecContext(ctx,
		`DELETE FROM model_versions WHERE id = $1 AND owner_id = $2`, id, ownerID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return httpx.ErrNotFound("model version not found")
	}
	return nil
}

func rowToView(r *modelRow) *View {
	return &View{
		ID: r.ID, OwnerID: r.OwnerID, Name: r.Name, Version: r.Version,
		InputDim: r.InputDim, Classes: r.Classes, ClassesHash: r.ClassesHash,
		Weights: r.Weights, WeightSHA256: r.WeightSHA256,
		TrainDatasetSHA: r.TrainDatasetSHA, EvalDatasetSHA: r.EvalDatasetSHA,
		Metrics: r.Metrics, CreatedAt: r.CreatedAt,
	}
}

// PredictRequest carries one feature vector.
type PredictRequest struct {
	X []float64 `json:"x"`
}

// PredictResponse carries real computed probabilities plus the chosen class.
type PredictResponse struct {
	ModelID        int64     `json:"model_id"`
	PredictedClass int       `json:"predicted_class"`
	ClassName      string    `json:"class_name"`
	Probabilities  []float64 `json:"probabilities"`
	ExperimentID   int64     `json:"experiment_id"`
}

// Predict reloads the stored weights (never a hardcoded predictor), runs the
// float64 CPU forward pass, records the experiment and returns the result.
func (s *Service) Predict(ctx context.Context, ownerID, modelID int64, req *PredictRequest) (*PredictResponse, error) {
	if req.X == nil {
		return nil, httpx.ErrBadRequest("x is required")
	}
	var r modelRow
	err := s.DB.GetContext(ctx, &r, `
		SELECT id, owner_id, name, version, input_dim, classes, classes_hash,
		       weights, weight_sha256, train_dataset_sha, eval_dataset_sha,
		       metrics, created_at
		FROM model_versions WHERE id = $1 AND owner_id = $2`, modelID, ownerID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, httpx.ErrNotFound("model version not found")
		}
		return nil, err
	}
	weights, err := spec.ParseWeights(r.Weights)
	if err != nil {
		return nil, fmt.Errorf("stored weights are invalid: %w", err)
	}
	model := nn.FromWeights(weights)
	probs, idx, name, err := model.Predict(req.X)
	if err != nil {
		return nil, httpx.ErrUnprocess(err.Error())
	}

	inputJSON, _ := json.Marshal(req.X)
	probsJSON, _ := json.Marshal(probs)
	var expID int64
	if err := s.DB.GetContext(ctx, &expID, `
		INSERT INTO experiments(owner_id, model_version_id, input, probs, predicted_class)
		VALUES ($1,$2,$3,$4,$5) RETURNING id`,
		ownerID, modelID, inputJSON, probsJSON, idx); err != nil {
		return nil, err
	}
	return &PredictResponse{
		ModelID: modelID, PredictedClass: idx, ClassName: name,
		Probabilities: probs, ExperimentID: expID,
	}, nil
}
