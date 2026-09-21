// Package modelx manages immutable model versions bound to published datasets.
package modelx

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jmoiron/sqlx"

	"synapticgo/internal/inference"
	"synapticgo/internal/models"
)

var (
	ErrNotFound        = errors.New("model version not found")
	ErrForbidden       = errors.New("not the model owner")
	ErrDatasetNotReady = errors.New("dataset is not published")
	ErrClassMismatch   = errors.New("class count does not match weights")
	ErrClassesInvalid  = errors.New("classes must be a non-empty list of unique labels with at least two entries")
	ErrInUse           = errors.New("model version has experiment records")
)

type Service struct {
	db *sqlx.DB
}

func NewService(db *sqlx.DB) *Service { return &Service{db: db} }

// RegisterInput is a registration request.
type RegisterInput struct {
	ModelName string             `json:"model_name"`
	DatasetID int64              `json:"dataset_id"`
	InputDim  int                `json:"input_dim"`
	Classes   []string           `json:"classes"`
	Weights   *inference.Weights `json:"-"`
}

// ClassTableDigest returns the canonical digest of the ordered class table.
// Comparisons between versions are only valid when digests are equal.
func ClassTableDigest(classes []string) string {
	canonical, _ := json.Marshal(classes)
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

// Register validates the weights against input_dim and the class table, binds
// the version to the dataset's current whole-file digest, and stores an
// immutable row. Version numbers auto-increment per (owner, model_name).
func (s *Service) Register(ctx context.Context, callerID int64, in RegisterInput) (*models.ModelVersion, error) {
	if in.ModelName == "" {
		return nil, fmt.Errorf("model_name required")
	}
	if in.InputDim <= 0 {
		return nil, fmt.Errorf("input_dim must be > 0")
	}
	if len(in.Classes) < 2 {
		return nil, ErrClassesInvalid
	}
	seen := make(map[string]struct{}, len(in.Classes))
	for _, c := range in.Classes {
		if c == "" {
			return nil, ErrClassesInvalid
		}
		if _, dup := seen[c]; dup {
			return nil, ErrClassesInvalid
		}
		seen[c] = struct{}{}
	}
	if in.Weights == nil {
		return nil, fmt.Errorf("weights required")
	}
	if in.Weights.InputDim != in.InputDim {
		return nil, fmt.Errorf("weights expect input_dim %d, request declares %d", in.Weights.InputDim, in.InputDim)
	}
	if in.Weights.NumClasses != len(in.Classes) {
		return nil, fmt.Errorf("%w: %d classes but weights have %d outputs",
			ErrClassMismatch, len(in.Classes), in.Weights.NumClasses)
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	// Dataset must be owned by the caller and published; pin its digest.
	var d struct {
		OwnerID  int64          `db:"owner_id"`
		Status   string         `db:"status"`
		Digest   sql.NullString `db:"digest"`
		ObjectID sql.NullInt64  `db:"whole_object_id"`
	}
	err = tx.Get(&d,
		`SELECT owner_id, status, whole_object_id,
		        (SELECT o.digest FROM objects o WHERE o.id = datasets.whole_object_id) AS digest
		 FROM datasets WHERE id = $1 FOR SHARE`, in.DatasetID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrDatasetNotReady
	}
	if err != nil {
		return nil, err
	}
	if d.OwnerID != callerID {
		return nil, ErrForbidden
	}
	if d.Status != models.StatusReady || !d.ObjectID.Valid || !d.Digest.Valid {
		return nil, ErrDatasetNotReady
	}

	// Serialize concurrent registrations for the same (owner, model_name) with
	// a transaction-scoped advisory lock, so version numbers never race.
	if _, err := tx.Exec(`SELECT pg_advisory_xact_lock(hashtext($1)::bigint)`,
		fmt.Sprintf("model:%d:%s", callerID, in.ModelName)); err != nil {
		return nil, err
	}
	var nextVersion int32
	if err := tx.Get(&nextVersion,
		`SELECT COALESCE(max(version), 0) + 1 FROM model_versions
		 WHERE owner_id = $1 AND model_name = $2`,
		callerID, in.ModelName); err != nil {
		return nil, err
	}

	classJSON, _ := json.Marshal(in.Classes)
	encoded := in.Weights.Encode()
	weightSum := sha256.Sum256(encoded)

	var mv models.ModelVersion
	err = tx.Get(&mv,
		`INSERT INTO model_versions
		   (owner_id, model_name, version, dataset_id, dataset_digest,
		    input_dim, num_classes, classes, class_table_digest, weights, weight_digest)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
		 RETURNING id, owner_id, model_name, version, dataset_id, dataset_digest,
		           input_dim, num_classes, classes, class_table_digest, weights,
		           weight_digest, created_at`,
		callerID, in.ModelName, nextVersion, in.DatasetID, d.Digest.String,
		in.InputDim, len(in.Classes), classJSON, ClassTableDigest(in.Classes),
		encoded, hex.EncodeToString(weightSum[:]))
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &mv, nil
}

// Get fetches a model version for its owner.
func (s *Service) Get(ctx context.Context, callerID, id int64) (*models.ModelVersion, error) {
	return s.getOwned(ctx, id, callerID)
}

// LoadForInference returns a version regardless of caller (inference is open to
// any authenticated user) plus decoded weights.
func (s *Service) LoadForInference(ctx context.Context, id int64) (*models.ModelVersion, *inference.Weights, error) {
	mv, err := s.get(ctx, id)
	if err != nil {
		return nil, nil, err
	}
	w, err := inference.Decode(mv.Weights)
	if err != nil {
		return nil, nil, err
	}
	return mv, w, nil
}

func (s *Service) get(ctx context.Context, id int64) (*models.ModelVersion, error) {
	var mv models.ModelVersion
	err := s.db.GetContext(ctx, &mv,
		`SELECT id, owner_id, model_name, version, dataset_id, dataset_digest,
		        input_dim, num_classes, classes, class_table_digest, weights,
		        weight_digest, created_at
		 FROM model_versions WHERE id = $1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &mv, nil
}

func (s *Service) getOwned(ctx context.Context, id, callerID int64) (*models.ModelVersion, error) {
	mv, err := s.get(ctx, id)
	if err != nil {
		return nil, err
	}
	if mv.OwnerID != callerID {
		return nil, ErrForbidden
	}
	return mv, nil
}

// List returns the caller's model versions.
func (s *Service) List(ctx context.Context, callerID int64, modelName string, limit, offset int) ([]models.ModelVersion, error) {
	var out []models.ModelVersion
	q := `SELECT id, owner_id, model_name, version, dataset_id, dataset_digest,
	             input_dim, num_classes, classes, class_table_digest, weights,
	             weight_digest, created_at
	      FROM model_versions WHERE owner_id = $1`
	args := []any{callerID}
	if modelName != "" {
		q += ` AND model_name = $2`
		args = append(args, modelName)
	}
	q += ` ORDER BY id DESC LIMIT ` + fmt.Sprint(limit) + ` OFFSET ` + fmt.Sprint(offset)
	if err := s.db.SelectContext(ctx, &out, q, args...); err != nil {
		return nil, err
	}
	return out, nil
}

// Delete removes a model version. Experiment records reference it with ON
// DELETE CASCADE, but deletion is still refused while records exist, so run
// history cannot silently disappear.
func (s *Service) Delete(ctx context.Context, callerID, id int64) error {
	mv, err := s.getOwned(ctx, id, callerID)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`SELECT id FROM model_versions WHERE id = $1 FOR UPDATE`, mv.ID); err != nil {
		return err
	}
	var n int
	if err := tx.Get(&n, `SELECT count(*) FROM experiment_records WHERE model_version_id = $1`, mv.ID); err != nil {
		return err
	}
	if n > 0 {
		return ErrInUse
	}
	if _, err := tx.Exec(`DELETE FROM model_versions WHERE id = $1`, mv.ID); err != nil {
		return err
	}
	return tx.Commit()
}
