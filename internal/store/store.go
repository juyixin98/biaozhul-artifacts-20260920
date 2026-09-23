// Package store 封装 PostgreSQL 数据访问。
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"rollout/internal/engine"
)

type DBTX interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

var _ DBTX = (*pgxpool.Pool)(nil)

type Store struct {
	Pool *pgxpool.Pool
}

func Open(ctx context.Context, url string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return &Store{Pool: pool}, nil
}

func (s *Store) Close() { s.Pool.Close() }

// ApplyMigrations 执行指定 SQL 文件（幂等：DDL 均带 IF NOT EXISTS）。
func (s *Store) ApplyMigrations(ctx context.Context, path string) error {
	sqlBytes, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	_, err = s.Pool.Exec(ctx, string(sqlBytes))
	return err
}

type Rollout struct {
	ID                       int64             `json:"id"`
	Name                     string            `json:"name"`
	Service                  string            `json:"service"`
	Status                   string            `json:"status"`
	StageIdx                 int               `json:"stage_idx"`
	StageWeight              int               `json:"stage_weight"`
	Generation               int64             `json:"generation"`
	ThresholdVersionID       int64             `json:"threshold_version_id"`
	Threshold                engine.Thresholds `json:"threshold"`
	StageStartedAt           time.Time         `json:"stage_started_at"`
	MinSamples               int64             `json:"min_samples"`
	ObservationWindowSeconds int64             `json:"observation_window_seconds"`
	CreatedAt                time.Time         `json:"created_at"`
	UpdatedAt                time.Time         `json:"updated_at"`
}

type Decision struct {
	ID                 int64           `json:"id"`
	RolloutID          int64           `json:"rollout_id"`
	StageIdx           int             `json:"stage_idx"`
	Generation         int64           `json:"generation"`
	Verdict            string          `json:"verdict"`
	Reason             string          `json:"reason"`
	WindowStart        time.Time       `json:"window_start"`
	WindowEnd          time.Time       `json:"window_end"`
	WindowComplete     bool            `json:"window_complete"`
	Samples            int64           `json:"samples"`
	ErrorRate          *float64        `json:"error_rate"`
	P95LatencyMs       *float64        `json:"p95_latency_ms"`
	Coverage           float64         `json:"coverage"`
	MetricsStatus      string          `json:"metrics_status"`
	Buckets            []engine.Bucket `json:"buckets"`
	ThresholdVersionID int64           `json:"threshold_version_id"`
	CreatedAt          time.Time       `json:"created_at"`
}

type Command struct {
	ID                 int64           `json:"id"`
	Type               string          `json:"type"`
	ExpectedGeneration int64           `json:"expected_generation"`
	Status             string          `json:"status"`
	HTTPStatus         int             `json:"http_status"`
	RequestHash        string          `json:"request_hash"`
	Response           json.RawMessage `json:"response"`
	CreatedAt          time.Time       `json:"created_at"`
}

const rolloutSelect = `
SELECT r.id, r.name, r.service, r.status, r.stage_idx, r.generation, r.threshold_version_id,
       r.stage_started_at, r.min_samples, r.observation_window_seconds, r.created_at, r.updated_at,
       t.max_error_rate, t.max_p95_latency_ms
FROM rollouts r JOIN threshold_versions t ON t.id = r.threshold_version_id`

func scanRollout(row pgx.Row) (*Rollout, error) {
	var r Rollout
	err := row.Scan(
		&r.ID, &r.Name, &r.Service, &r.Status, &r.StageIdx, &r.Generation, &r.ThresholdVersionID,
		&r.StageStartedAt, &r.MinSamples, &r.ObservationWindowSeconds, &r.CreatedAt, &r.UpdatedAt,
		&r.Threshold.MaxErrorRate, &r.Threshold.MaxP95LatencyMs,
	)
	if err != nil {
		return nil, err
	}
	r.StageWeight = engine.StageWeights[r.StageIdx]
	return &r, nil
}

func (s *Store) GetRollout(ctx context.Context, id int64) (*Rollout, error) {
	return scanRollout(s.Pool.QueryRow(ctx, rolloutSelect+` WHERE r.id = $1`, id))
}

// GetRolloutForUpdate 必须在事务内调用，行锁串行化命令执行。
func (s *Store) GetRolloutForUpdate(ctx context.Context, tx pgx.Tx, id int64) (*Rollout, error) {
	return scanRollout(tx.QueryRow(ctx, rolloutSelect+` WHERE r.id = $1 FOR UPDATE`, id))
}

func (s *Store) ListRollouts(ctx context.Context) ([]*Rollout, error) {
	rows, err := s.Pool.Query(ctx, rolloutSelect+` ORDER BY id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Rollout
	for rows.Next() {
		r, err := scanRollout(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// CreateRolloutParams 创建发布；阈值同时落库为新版本并在发布生命周期内冻结。
type CreateRolloutParams struct {
	Name                     string
	Service                  string
	MinSamples               int64
	ObservationWindowSeconds int64
	MaxErrorRate             float64
	MaxP95LatencyMs          float64
	Note                     string
}

func (s *Store) CreateRollout(ctx context.Context, p CreateRolloutParams) (*Rollout, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	var tvID int64
	err = tx.QueryRow(ctx,
		`INSERT INTO threshold_versions (max_error_rate, max_p95_latency_ms, note)
		 VALUES ($1,$2,$3) RETURNING id`,
		p.MaxErrorRate, p.MaxP95LatencyMs, p.Note).Scan(&tvID)
	if err != nil {
		return nil, err
	}
	var newID int64
	err = tx.QueryRow(ctx, `
		INSERT INTO rollouts (name, service, threshold_version_id, stage_started_at, min_samples, observation_window_seconds)
		VALUES ($1,$2,$3,now(),$4,$5)
		RETURNING id`,
		p.Name, p.Service, tvID, p.MinSamples, p.ObservationWindowSeconds).Scan(&newID)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return s.GetRollout(ctx, newID)
}

// AdvanceStage 在事务内推进/完成发布并推进代次。
func (s *Store) AdvanceStage(ctx context.Context, tx pgx.Tx, id int64, newIdx int, completed bool) error {
	status := "active"
	if completed {
		status = "completed"
	}
	_, err := tx.Exec(ctx, `
		UPDATE rollouts
		   SET stage_idx = $2, status = $3, generation = generation + 1,
		       stage_started_at = now(), updated_at = now()
		 WHERE id = $1`, id, newIdx, status)
	return err
}

// SetStatus 在事务内改变状态（pause/rollback）并推进代次。
func (s *Store) SetStatus(ctx context.Context, tx pgx.Tx, id int64, status string) error {
	_, err := tx.Exec(ctx, `
		UPDATE rollouts SET status = $2, generation = generation + 1, updated_at = now()
		WHERE id = $1`, id, status)
	return err
}

// InsertDecision 持久化一次判定及其指标区间证据。
func (s *Store) InsertDecision(ctx context.Context, q DBTX, r *Rollout, d *engine.Decision,
	windowStart, windowEnd time.Time, windowComplete bool) (*Decision, error) {
	buckets, err := json.Marshal(d.Buckets)
	if err != nil {
		return nil, err
	}
	rec := &Decision{
		RolloutID: r.ID, StageIdx: r.StageIdx, Generation: r.Generation,
		Verdict: d.Verdict, Reason: d.Reason,
		WindowStart: windowStart, WindowEnd: windowEnd, WindowComplete: windowComplete,
		Samples: d.Samples, ErrorRate: d.ErrorRate, P95LatencyMs: d.P95LatencyMs,
		Coverage: d.Coverage, MetricsStatus: d.MetricsStatus,
		Buckets: d.Buckets, ThresholdVersionID: r.ThresholdVersionID,
	}
	var stored []byte
	err = q.QueryRow(ctx, `
		INSERT INTO decisions (rollout_id, stage_idx, generation, verdict, reason,
			window_start, window_end, window_complete, samples, error_rate, p95_latency_ms,
			coverage, metrics_status, buckets, threshold_version_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
		RETURNING id, created_at, buckets`,
		r.ID, r.StageIdx, r.Generation, d.Verdict, d.Reason,
		windowStart, windowEnd, windowComplete,
		d.Samples, d.ErrorRate, d.P95LatencyMs,
		d.Coverage, d.MetricsStatus, buckets, r.ThresholdVersionID,
	).Scan(&rec.ID, &rec.CreatedAt, &stored)
	if err != nil {
		return nil, err
	}
	if len(stored) > 0 {
		_ = json.Unmarshal(stored, &rec.Buckets)
	}
	return rec, nil
}

func (s *Store) ListDecisions(ctx context.Context, rolloutID int64, limit int) ([]*Decision, error) {
	if limit <= 0 || limit > 200 {
		limit = 20
	}
	rows, err := s.Pool.Query(ctx, `
		SELECT id, rollout_id, stage_idx, generation, verdict, reason,
		       window_start, window_end, window_complete, samples, error_rate, p95_latency_ms,
		       coverage, metrics_status, buckets, threshold_version_id, created_at
		FROM decisions WHERE rollout_id = $1 ORDER BY id DESC LIMIT $2`, rolloutID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanDecisions(rows)
}

func scanDecisions(rows pgx.Rows) ([]*Decision, error) {
	var out []*Decision
	for rows.Next() {
		var d Decision
		var buckets []byte
		if err := rows.Scan(
			&d.ID, &d.RolloutID, &d.StageIdx, &d.Generation, &d.Verdict, &d.Reason,
			&d.WindowStart, &d.WindowEnd, &d.WindowComplete, &d.Samples,
			&d.ErrorRate, &d.P95LatencyMs, &d.Coverage, &d.MetricsStatus,
			&buckets, &d.ThresholdVersionID, &d.CreatedAt,
		); err != nil {
			return nil, err
		}
		_ = json.Unmarshal(buckets, &d.Buckets)
		out = append(out, &d)
	}
	return out, rows.Err()
}

// FindCommand 查找已执行命令（幂等重放）。
func (s *Store) FindCommand(ctx context.Context, q DBTX, rolloutID int64, key string) (*Command, error) {
	var c Command
	var resp []byte
	err := q.QueryRow(ctx, `
		SELECT id, type, expected_generation, status, http_status, request_hash, response, created_at
		FROM commands WHERE rollout_id = $1 AND idempotency_key = $2`,
		rolloutID, key).Scan(
		&c.ID, &c.Type, &c.ExpectedGeneration, &c.Status, &c.HTTPStatus, &c.RequestHash, &resp, &c.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	c.Response = resp
	return &c, nil
}

// InsertCommand 持久化命令结果。唯一冲突时返回 pg 唯一违例，由上层处理。
func (s *Store) InsertCommand(ctx context.Context, q DBTX, rolloutID int64,
	cmdType string, expectedGen int64, key, reqHash, status string, httpStatus int, response any) error {
	body, err := json.Marshal(response)
	if err != nil {
		return err
	}
	_, err = q.Exec(ctx, `
		INSERT INTO commands (rollout_id, idempotency_key, type, expected_generation, request_hash, status, http_status, response)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		rolloutID, key, cmdType, expectedGen, reqHash, status, httpStatus, body)
	return err
}

// IsUniqueViolation 判断是否唯一约束冲突（并发幂等键竞争）。
func IsUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

var ErrNotFound = fmt.Errorf("not found")
