package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/inference"
)

type ModelService struct {
	DB *sqlx.DB
}

type CreateModelInput struct {
	Name string `json:"name"`
}

func (s *ModelService) Create(ctx context.Context, owner string, in CreateModelInput) (*Model, error) {
	if in.Name == "" {
		return nil, BadRequestf("name is required")
	}
	var m Model
	err := s.DB.GetContext(ctx, &m,
		`INSERT INTO models (owner_id, name) VALUES ($1,$2) RETURNING *`, owner, in.Name)
	if err != nil {
		return nil, err
	}
	return &m, nil
}

func (s *ModelService) List(ctx context.Context, owner string) ([]Model, error) {
	models := []Model{}
	err := s.DB.SelectContext(ctx, &models,
		`SELECT * FROM models WHERE owner_id=$1 ORDER BY id`, owner)
	return models, err
}

type ModelView struct {
	Model
	Versions []ModelVersion `json:"versions"`
}

func (s *ModelService) getModel(ctx context.Context, owner string, id int64) (*Model, error) {
	var m Model
	err := s.DB.GetContext(ctx, &m, `SELECT * FROM models WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("model %d not found", id)
	}
	if err != nil {
		return nil, err
	}
	if m.OwnerID != owner {
		return nil, NotFoundf("model %d not found", id)
	}
	return &m, nil
}

func (s *ModelService) Get(ctx context.Context, owner string, id int64) (*ModelView, error) {
	m, err := s.getModel(ctx, owner, id)
	if err != nil {
		return nil, err
	}
	versions := []ModelVersion{}
	if err := s.DB.SelectContext(ctx, &versions,
		`SELECT * FROM model_versions WHERE model_id=$1 ORDER BY version`, id); err != nil {
		return nil, err
	}
	return &ModelView{Model: *m, Versions: versions}, nil
}

// Delete refuses while any experiment references one of the model's versions.
func (s *ModelService) Delete(ctx context.Context, owner string, id int64) error {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var m Model
	err = tx.GetContext(ctx, &m, `SELECT * FROM models WHERE id=$1 FOR UPDATE`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return NotFoundf("model %d not found", id)
	}
	if err != nil {
		return err
	}
	if m.OwnerID != owner {
		return NotFoundf("model %d not found", id)
	}
	var refs int
	if err := tx.GetContext(ctx, &refs, `
		SELECT count(*) FROM experiments e
		JOIN model_versions mv ON mv.id = e.model_version_id
		WHERE mv.model_id=$1`, id); err != nil {
		return err
	}
	if refs > 0 {
		return Conflictf("model %d is still referenced by %d experiment(s)", id, refs)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM models WHERE id=$1`, id); err != nil {
		return err
	}
	return tx.Commit()
}

type CreateVersionInput struct {
	DatasetID int64       `json:"dataset_id"`
	InputDim  int         `json:"input_dim"`
	Labels    []string    `json:"labels"`
	Weights   [][]float64 `json:"weights"`
	Bias      []float64   `json:"bias"`
}

type weightsDoc struct {
	Weights [][]float64 `json:"weights"`
	Bias    []float64   `json:"bias"`
}

// CreateVersion binds an immutable version to a published dataset digest.
// The model row lock serializes version-number allocation.
func (s *ModelService) CreateVersion(ctx context.Context, owner string, modelID int64, in CreateVersionInput) (*ModelVersion, error) {
	if in.InputDim <= 0 {
		return nil, BadRequestf("input_dim must be positive")
	}
	lm := inference.LinearModel{Labels: in.Labels, Weights: in.Weights, Bias: in.Bias}
	if err := lm.Validate(); err != nil {
		return nil, BadRequestf("invalid model parameters: %v", err)
	}
	if lm.InputDim() != in.InputDim {
		return nil, BadRequestf("input_dim %d does not match weights columns %d", in.InputDim, lm.InputDim())
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	var m Model
	err = tx.GetContext(ctx, &m, `SELECT * FROM models WHERE id=$1 FOR UPDATE`, modelID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("model %d not found", modelID)
	}
	if err != nil {
		return nil, err
	}
	if m.OwnerID != owner {
		return nil, NotFoundf("model %d not found", modelID)
	}

	var ds Dataset
	err = tx.GetContext(ctx, &ds, `SELECT * FROM datasets WHERE id=$1`, in.DatasetID)
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

	labelsJSON, _ := json.Marshal(in.Labels)
	weightsJSON, _ := json.Marshal(weightsDoc{Weights: in.Weights, Bias: in.Bias})
	var mv ModelVersion
	if err := tx.GetContext(ctx, &mv, `
		INSERT INTO model_versions (model_id, version, dataset_id, dataset_sha256, input_dim, labels, weights)
		SELECT $1, COALESCE(MAX(version),0)+1, $2, $3, $4, $5::jsonb, $6::jsonb
		FROM model_versions WHERE model_id=$1
		RETURNING *`,
		modelID, ds.ID, ds.SHA256, in.InputDim, string(labelsJSON), string(weightsJSON)); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &mv, nil
}

func (s *ModelService) GetVersion(ctx context.Context, owner string, modelID int64, version int) (*ModelVersion, error) {
	var mv ModelVersion
	err := s.DB.GetContext(ctx, &mv, `
		SELECT mv.* FROM model_versions mv
		JOIN models m ON m.id = mv.model_id
		WHERE mv.model_id=$1 AND mv.version=$2 AND m.owner_id=$3`, modelID, version, owner)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, NotFoundf("model %d version %d not found", modelID, version)
	}
	if err != nil {
		return nil, err
	}
	return &mv, nil
}

// Predict runs the real CPU forward pass for the stored weights.
func (s *ModelService) Predict(ctx context.Context, owner string, modelID int64, version int, inputs [][]float64) ([]inference.Prediction, error) {
	mv, err := s.GetVersion(ctx, owner, modelID, version)
	if err != nil {
		return nil, err
	}
	var doc weightsDoc
	if err := json.Unmarshal(mv.Weights, &doc); err != nil {
		return nil, err
	}
	var labels []string
	if err := json.Unmarshal(mv.Labels, &labels); err != nil {
		return nil, err
	}
	lm := inference.LinearModel{Labels: labels, Weights: doc.Weights, Bias: doc.Bias}
	if err := lm.Validate(); err != nil {
		return nil, err
	}
	preds, err := lm.PredictBatch(inputs)
	if err != nil {
		return nil, BadRequestf("invalid inputs: %v", err)
	}
	return preds, nil
}

type VersionReport struct {
	Version     ModelVersion       `json:"version"`
	Experiments []Experiment       `json:"experiments"`
	MetricMeans map[string]float64 `json:"metric_means"`
}

type CompareResult struct {
	Comparable  bool               `json:"comparable"`
	Reason      string             `json:"reason,omitempty"`
	Versions    []VersionReport    `json:"versions"`
	MetricDiffs map[string]float64 `json:"metric_diffs,omitempty"`
}

// Compare reports two versions side by side. Metric diffs are only produced
// when both versions use the identical label table; otherwise the metrics
// are not comparable and only raw data is returned.
func (s *ModelService) Compare(ctx context.Context, owner string, modelID int64, v1, v2 int) (*CompareResult, error) {
	if _, err := s.getModel(ctx, owner, modelID); err != nil {
		return nil, err
	}
	mv1, err := s.GetVersion(ctx, owner, modelID, v1)
	if err != nil {
		return nil, err
	}
	mv2, err := s.GetVersion(ctx, owner, modelID, v2)
	if err != nil {
		return nil, err
	}

	res := &CompareResult{Versions: make([]VersionReport, 0, 2)}
	means := make([]map[string]float64, 0, 2)
	for _, mv := range []*ModelVersion{mv1, mv2} {
		exps := []Experiment{}
		if err := s.DB.SelectContext(ctx, &exps,
			`SELECT * FROM experiments WHERE model_version_id=$1 AND owner_id=$2 ORDER BY id`, mv.ID, owner); err != nil {
			return nil, err
		}
		mean := metricMeans(exps)
		res.Versions = append(res.Versions, VersionReport{Version: *mv, Experiments: exps, MetricMeans: mean})
		means = append(means, mean)
	}

	var l1, l2 []string
	if err := json.Unmarshal(mv1.Labels, &l1); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(mv2.Labels, &l2); err != nil {
		return nil, err
	}
	if !reflect.DeepEqual(l1, l2) {
		res.Comparable = false
		res.Reason = "versions use different label tables; metrics are not comparable"
		return res, nil
	}
	res.Comparable = true
	res.MetricDiffs = map[string]float64{}
	for k, a := range means[0] {
		if b, ok := means[1][k]; ok {
			res.MetricDiffs[k] = b - a
		}
	}
	return res, nil
}

// metricMeans averages numeric metric values across experiments; non-numeric
// values are ignored.
func metricMeans(exps []Experiment) map[string]float64 {
	sums := map[string]float64{}
	counts := map[string]int{}
	for _, e := range exps {
		var obj map[string]json.RawMessage
		if err := json.Unmarshal(e.Metrics, &obj); err != nil {
			continue
		}
		for k, raw := range obj {
			var v float64
			if err := json.Unmarshal(raw, &v); err != nil {
				continue
			}
			sums[k] += v
			counts[k]++
		}
	}
	means := map[string]float64{}
	for k, s := range sums {
		means[k] = s / float64(counts[k])
	}
	return means
}
