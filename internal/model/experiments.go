package modelreg

import (
	"context"
	"encoding/json"
)

// ExperimentView is one recorded inference.
type ExperimentView struct {
	ID             int64           `json:"id" db:"id"`
	ModelVersionID int64           `json:"model_version_id" db:"model_version_id"`
	Input          json.RawMessage `json:"input" db:"input"`
	Probs          json.RawMessage `json:"probs" db:"probs"`
	PredictedClass int             `json:"predicted_class" db:"predicted_class"`
	CreatedAt      string          `json:"created_at" db:"created_at"`
}

type experimentRow struct {
	ID             int64           `db:"id"`
	ModelVersionID int64           `db:"model_version_id"`
	Input          json.RawMessage `db:"input"`
	Probs          json.RawMessage `db:"probs"`
	PredictedClass int             `db:"predicted_class"`
	CreatedAt      string          `db:"created_at"`
}

// ListExperiments returns inference records for the caller, optionally
// restricted to one model version. Records are returned newest-first.
func (s *Service) ListExperiments(ctx context.Context, ownerID int64, modelVersionID *int64, limit int, beforeID *int64) ([]ExperimentView, error) {
	if limit <= 0 {
		limit = 50
	}
	if limit > 200 {
		limit = 200
	}
	q := psql.Select(`id, model_version_id, input, probs, predicted_class,
			to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.USZ') AS created_at`).
		From("experiments").Where("owner_id = ?", ownerID)
	if modelVersionID != nil {
		q = q.Where("model_version_id = ?", *modelVersionID)
	}
	if beforeID != nil {
		q = q.Where("id < ?", *beforeID)
	}
	q = q.OrderBy("id DESC").Limit(uint64(limit))
	sqlStr, args, err := q.ToSql()
	if err != nil {
		return nil, err
	}
	var rows []experimentRow
	if err := s.DB.SelectContext(ctx, &rows, sqlStr, args...); err != nil {
		return nil, err
	}
	out := make([]ExperimentView, len(rows))
	for i := range rows {
		out[i] = ExperimentView(rows[i])
	}
	return out, nil
}
