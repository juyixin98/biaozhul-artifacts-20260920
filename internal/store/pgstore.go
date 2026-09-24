package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/example/rollout/internal/models"
)

// PG is the PostgreSQL implementation of Store.
type PG struct {
	pool *pgxpool.Pool
}

func NewPG(ctx context.Context, url string) (*PG, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	return &PG{pool: pool}, nil
}

func (p *PG) Close() { p.pool.Close() }

// Migrate creates the schema and seeds the v1 threshold policy, idempotently.
func (p *PG) Migrate(ctx context.Context) error {
	_, err := p.pool.Exec(ctx, schemaSQL)
	if err != nil {
		return fmt.Errorf("migration failed: %w", err)
	}
	// Seed default policy if no row exists.
	var n int
	if err := p.pool.QueryRow(ctx, `SELECT count(*) FROM threshold_versions`).Scan(&n); err != nil {
		return err
	}
	if n == 0 {
		spec, _ := json.Marshal(models.DefaultThresholdSpec())
		if _, err := p.pool.Exec(ctx,
			`INSERT INTO threshold_versions(version, spec, description, created_at)
			 VALUES (1, $1::jsonb, 'initial default policy', now())`, string(spec)); err != nil {
			return err
		}
		// Bring the bigserial sequence past the explicitly-inserted row so the
		// next CreateThreshold produces version 2, not a primary-key collision.
		if _, err := p.pool.Exec(ctx,
			`SELECT setval(pg_get_serial_sequence('threshold_versions','version'),
			       (SELECT COALESCE(max(version),1) FROM threshold_versions), true)`); err != nil {
			return err
		}
	}
	return nil
}

func (p *PG) Ping(ctx context.Context) error {
	return p.pool.Ping(ctx)
}

// ---------- thresholds ----------

func (p *PG) GetLatestThreshold(ctx context.Context) (models.ThresholdVersion, error) {
	row := p.pool.QueryRow(ctx,
		`SELECT version, spec, created_at, description FROM threshold_versions ORDER BY version DESC LIMIT 1`)
	return scanThreshold(row)
}

func (p *PG) GetThreshold(ctx context.Context, version int) (models.ThresholdVersion, error) {
	row := p.pool.QueryRow(ctx,
		`SELECT version, spec, created_at, description FROM threshold_versions WHERE version=$1`, version)
	return scanThreshold(row)
}

func (p *PG) CreateThreshold(ctx context.Context, spec models.ThresholdSpec, description string) (models.ThresholdVersion, error) {
	b, _ := json.Marshal(spec)
	row := p.pool.QueryRow(ctx,
		`INSERT INTO threshold_versions(spec, description, created_at)
		 VALUES ($1::jsonb, $2, now())
		 RETURNING version, spec, created_at, description`,
		string(b), description)
	return scanThreshold(row)
}

func scanThreshold(row pgx.Row) (models.ThresholdVersion, error) {
	var (
		t       models.ThresholdVersion
		specRaw string
	)
	if err := row.Scan(&t.Version, &specRaw, &t.CreatedAt, &t.Description); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return t, ErrNotFound
		}
		return t, err
	}
	if err := json.Unmarshal([]byte(specRaw), &t.Spec); err != nil {
		return t, fmt.Errorf("decoding threshold spec: %w", err)
	}
	return t, nil
}

// ---------- releases ----------

func (p *PG) CreateRelease(ctx context.Context, r models.Release) error {
	spec, _ := json.Marshal(r.ThresholdSpec)
	_, err := p.pool.Exec(ctx, `
		INSERT INTO releases
		(id, name, version, state, stage, stage_weight, generation, observation_ms, min_samples,
		 threshold_version, threshold_snapshot, metric_url, scenario, stage_entered_at, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,$15,$16)`,
		r.ID, r.Name, r.Version, string(r.State), string(r.Stage), r.StageWeight, r.Generation,
		r.ObservationMS, r.MinSamples, r.ThresholdVersion, string(spec), r.MetricURL, r.Scenario,
		r.StageEnteredAt, r.CreatedAt, r.UpdatedAt)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrConflict
		}
		return err
	}
	return nil
}

func (p *PG) GetRelease(ctx context.Context, id string) (models.Release, error) {
	row := p.pool.QueryRow(ctx, `SELECT `+releaseCols+` FROM releases WHERE id=$1`, id)
	return scanRelease(row)
}

func (p *PG) ListReleases(ctx context.Context) ([]models.Release, error) {
	rows, err := p.pool.Query(ctx, `SELECT `+releaseCols+` FROM releases ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []models.Release
	for rows.Next() {
		r, err := scanRelease(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

const releaseCols = `id, name, version, state, stage, stage_weight, generation, observation_ms, min_samples,
 threshold_version, threshold_snapshot, metric_url, scenario, stage_entered_at, created_at, updated_at`

func scanRelease(row pgx.Row) (models.Release, error) {
	var (
		r       models.Release
		state   string
		stage   string
		specRaw string
	)
	err := row.Scan(
		&r.ID, &r.Name, &r.Version, &state, &stage, &r.StageWeight, &r.Generation,
		&r.ObservationMS, &r.MinSamples, &r.ThresholdVersion, &specRaw, &r.MetricURL,
		&r.Scenario, &r.StageEnteredAt, &r.CreatedAt, &r.UpdatedAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return r, ErrNotFound
		}
		return r, err
	}
	r.State = models.State(state)
	r.Stage = models.Stage(stage)
	if err := json.Unmarshal([]byte(specRaw), &r.ThresholdSpec); err != nil {
		return r, fmt.Errorf("decoding release snapshot: %w", err)
	}
	return r, nil
}

// ---------- events ----------

func (p *PG) ListEvents(ctx context.Context, releaseID string) ([]models.Event, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT seq, release_id, generation, type, command, from_stage, to_stage, verdict, detail, created_at
		 FROM release_events WHERE release_id=$1 ORDER BY seq`, releaseID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []models.Event
	for rows.Next() {
		var (
			e                                  models.Event
			cmd, from, to, verdictRaw, detailR *string
		)
		if err := rows.Scan(&e.Seq, &e.ReleaseID, &e.Generation, &e.Type, &cmd, &from, &to, &verdictRaw, &detailR, &e.CreatedAt); err != nil {
			return nil, err
		}
		if cmd != nil {
			e.Command = models.Command(*cmd)
		}
		if from != nil {
			e.FromStage = models.Stage(*from)
		}
		if to != nil {
			e.ToStage = models.Stage(*to)
		}
		if detailR != nil {
			e.Detail = *detailR
		}
		if verdictRaw != nil && *verdictRaw != "" {
			var v models.Verdict
			if err := json.Unmarshal([]byte(*verdictRaw), &v); err == nil {
				e.Verdict = &v
			}
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ---------- observations ----------

func (p *PG) ListObservations(ctx context.Context, releaseID string) ([]Observation, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT id, release_id, stage, generation, window_start, window_end, superseded, verdict, created_at
		 FROM observations WHERE release_id=$1 ORDER BY id`, releaseID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Observation
	for rows.Next() {
		var (
			o         Observation
			stage     string
			ws, we    time.Time
			verdict   string
			createdAt time.Time
		)
		if err := rows.Scan(&o.ID, &o.ReleaseID, &stage, &o.Generation, &ws, &we, &o.Superseded, &verdict, &createdAt); err != nil {
			return nil, err
		}
		o.Stage = models.Stage(stage)
		o.WindowStart = ws.Format(time.RFC3339Nano)
		o.WindowEnd = we.Format(time.RFC3339Nano)
		o.CreatedAt = createdAt.Format(time.RFC3339Nano)
		if err := json.Unmarshal([]byte(verdict), &o.Verdict); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// ---------- nonces / idempotency ----------

// InsertObservationDirect inserts an observation outside a command transaction
// (used by read-only probes).
func (p *PG) InsertObservationDirect(ctx context.Context, o Observation) error {
	return p.WithTx(ctx, func(tx Tx) error { return tx.InsertObservation(ctx, o) })
}

// ConsumeNonce atomically registers a one-time nonce digest. It returns
// (true,nil) on first use and (false,nil) on replay. Expired nonces are
// replaced.
func (p *PG) ConsumeNonce(ctx context.Context, digest string, expiresAtUnix int64) (bool, error) {
	tx, err := p.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)

	var existing int64
	err = tx.QueryRow(ctx,
		`SELECT count(*) FROM used_nonces WHERE digest=$1 AND expires_at > to_timestamp($2)`,
		digest, time.Now().Unix()).Scan(&existing)
	if err != nil {
		return false, err
	}
	if existing > 0 {
		return false, nil
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO used_nonces(digest, expires_at) VALUES ($1, to_timestamp($2))
		 ON CONFLICT (digest) DO UPDATE SET expires_at=EXCLUDED.expires_at`,
		digest, expiresAtUnix); err != nil {
		return false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

func (p *PG) GetIdempotentResponse(ctx context.Context, digest string) (StoredResponse, bool, error) {
	var (
		status int
		body   string
	)
	err := p.pool.QueryRow(ctx,
		`SELECT response_status, response_body FROM idempotency_keys WHERE key_digest=$1`, digest).
		Scan(&status, &body)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return StoredResponse{}, false, nil
		}
		return StoredResponse{}, false, err
	}
	return StoredResponse{Status: status, Body: []byte(body)}, true, nil
}

func (p *PG) PutIdempotentResponse(ctx context.Context, digest string, resp StoredResponse) error {
	_, err := p.pool.Exec(ctx,
		`INSERT INTO idempotency_keys(key_digest, response_status, response_body, created_at)
		 VALUES ($1,$2,$3, now())
		 ON CONFLICT (key_digest) DO UPDATE SET response_status=EXCLUDED.response_status,
		                                        response_body=EXCLUDED.response_body`,
		digest, resp.Status, string(resp.Body))
	return err
}

// ---------- transactional command path ----------

type pgTx struct{ tx pgx.Tx }

func (p *PG) WithTx(ctx context.Context, fn func(tx Tx) error) error {
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := fn(&pgTx{tx: tx}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (t *pgTx) GetReleaseForUpdate(ctx context.Context, id string) (models.Release, error) {
	row := t.tx.QueryRow(ctx, `SELECT `+releaseCols+` FROM releases WHERE id=$1 FOR UPDATE`, id)
	return scanRelease(row)
}

func (t *pgTx) UpdateRelease(ctx context.Context, r models.Release) error {
	spec, _ := json.Marshal(r.ThresholdSpec)
	ct, err := t.tx.Exec(ctx, `
		UPDATE releases SET state=$2, stage=$3, stage_weight=$4, generation=$5,
			threshold_snapshot=$6::jsonb, stage_entered_at=$7, updated_at=$8
		WHERE id=$1`,
		r.ID, string(r.State), string(r.Stage), r.StageWeight, r.Generation, string(spec),
		r.StageEnteredAt, r.UpdatedAt)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (t *pgTx) InsertEvent(ctx context.Context, e models.Event) error {
	var cmd, from, to, verdict *string
	if e.Command != "" {
		s := string(e.Command)
		cmd = &s
	}
	if e.FromStage != "" {
		s := string(e.FromStage)
		from = &s
	}
	if e.ToStage != "" {
		s := string(e.ToStage)
		to = &s
	}
	if e.Verdict != nil {
		b, _ := json.Marshal(e.Verdict)
		s := string(b)
		verdict = &s
	}
	// detail is NOT NULL DEFAULT ''; always bind a string (empty when absent).
	detail := e.Detail
	if e.CreatedAt.IsZero() {
		e.CreatedAt = time.Now().UTC()
	}
	_, err := t.tx.Exec(ctx, `
		INSERT INTO release_events
		(release_id, generation, type, command, from_stage, to_stage, verdict, detail, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)`,
		e.ReleaseID, e.Generation, e.Type, cmd, from, to, verdict, detail, e.CreatedAt)
	return err
}

func (t *pgTx) InsertObservation(ctx context.Context, o Observation) error {
	from, _ := time.Parse(time.RFC3339Nano, o.WindowStart)
	to, _ := time.Parse(time.RFC3339Nano, o.WindowEnd)
	verdict, _ := json.Marshal(o.Verdict)
	_, err := t.tx.Exec(ctx, `
		INSERT INTO observations
		(release_id, stage, generation, window_start, window_end, superseded, verdict, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb, now())`,
		o.ReleaseID, string(o.Stage), o.Generation, from, to, o.Superseded, string(verdict))
	return err
}

func isUniqueViolation(err error) bool {
	return err != nil && strings.Contains(err.Error(), "23505")
}
