// Package store persists mapping specs and conversion audit records in
// PostgreSQL. Mapping activation is transactional and announced through
// LISTEN/NOTIFY so all gateway instances hot-switch together.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/example/compgw/internal/mapping"
)

//go:embed migrations/0001_init.sql
var migrationSQL string

// Store wraps a pgx connection pool.
type Store struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 8
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close()              { s.pool.Close() }
func (s *Store) Pool() *pgxpool.Pool { return s.pool }

func (s *Store) Migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, migrationSQL)
	if err != nil {
		return fmt.Errorf("run migrations: %w", err)
	}
	return nil
}

// Seed upserts the given specs and activates one mapping per direction when
// no active mapping exists yet. Idempotent.
func (s *Store) Seed(ctx context.Context, specs []mapping.Spec) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	for _, sp := range specs {
		_, err := tx.Exec(ctx, `
			INSERT INTO mappings (name, direction, version, is_active, spec_json)
			VALUES ($1, $2, 1, FALSE, $3)
			ON CONFLICT (name) DO NOTHING`,
			sp.Name, sp.Direction, string(sp.JSON()))
		if err != nil {
			return fmt.Errorf("seed mapping %q: %w", sp.Name, err)
		}
	}

	// Activate defaults by name if the direction has nothing active.
	defaults := map[string]string{
		mapping.DirectionV1ToV2: "strict-v1-v2",
		mapping.DirectionV2ToV1: "strict-v2-v1",
	}
	for dir, name := range defaults {
		var active bool
		err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM mappings WHERE direction=$1 AND is_active)`, dir).Scan(&active)
		if err != nil {
			return err
		}
		if !active {
			if err := activateInTx(ctx, tx, name, nil); err != nil {
				return err
			}
		}
	}
	return tx.Commit(ctx)
}

// ActiveMapping is an immutable snapshot of the active mapping for one
// direction. Streams pin the snapshot they started with.
type ActiveMapping struct {
	Name    string
	Version uint32
	Spec    *mapping.Spec
}

func (s *Store) Active(ctx context.Context, direction string) (*ActiveMapping, error) {
	row := s.pool.QueryRow(ctx, `
		SELECT name, version, spec_json FROM mappings
		WHERE direction=$1 AND is_active`, direction)
	var name, raw string
	var version uint32
	if err := row.Scan(&name, &version, &raw); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("no active mapping for direction %s", direction)
		}
		return nil, err
	}
	spec, err := mapping.ParseSpec([]byte(raw))
	if err != nil {
		return nil, fmt.Errorf("stored mapping %q is invalid: %w", name, err)
	}
	return &ActiveMapping{Name: name, Version: version, Spec: spec}, nil
}

// ActivateByName switches the active mapping of its direction to an existing
// mapping by name, atomically bumping its version when the spec changes.
func (s *Store) ActivateByName(ctx context.Context, name string) (*ActiveMapping, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := activateInTx(ctx, tx, name, nil); err != nil {
		return nil, err
	}
	am, err := activeInTx(ctx, tx, name)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return am, nil
}

// UpsertAndActivate stores specJSON under its declared name and activates it.
func (s *Store) UpsertAndActivate(ctx context.Context, specJSON []byte) (*ActiveMapping, error) {
	sp, err := mapping.ParseSpec(specJSON)
	if err != nil {
		return nil, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	var existingJSON string
	err = tx.QueryRow(ctx, `SELECT spec_json FROM mappings WHERE name=$1`, sp.Name).Scan(&existingJSON)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		_, err = tx.Exec(ctx, `
			INSERT INTO mappings (name, direction, version, is_active, spec_json)
			VALUES ($1,$2,1,FALSE,$3)`, sp.Name, sp.Direction, string(sp.JSON()))
		if err != nil {
			return nil, err
		}
	case err != nil:
		return nil, err
	default:
		// Spec content changed -> bump version; unchanged -> keep.
		if existingJSON != string(sp.JSON()) {
			_, err = tx.Exec(ctx,
				`UPDATE mappings SET spec_json=$2, version=version+1, updated_at=now() WHERE name=$1`,
				sp.Name, string(sp.JSON()))
			if err != nil {
				return nil, err
			}
		}
	}

	if err := activateInTx(ctx, tx, sp.Name, nil); err != nil {
		return nil, err
	}
	am, err := activeInTx(ctx, tx, sp.Name)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return am, nil
}

func activateInTx(ctx context.Context, tx pgx.Tx, name string, _ any) error {
	var dir string
	err := tx.QueryRow(ctx, `SELECT direction FROM mappings WHERE name=$1`, name).Scan(&dir)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("mapping %q not found", name)
	}
	if err != nil {
		return err
	}
	// Exactly one row of the direction may be active at a time (partial unique
	// index). Note: in a single UPDATE the RHS sees the OLD row values, so we
	// perform the transitions explicitly, deactivating every row first (the
	// index permits zero active rows as an intermediate state).
	if _, err := tx.Exec(ctx,
		`UPDATE mappings SET is_active = FALSE, updated_at = now() WHERE direction = $1`, dir); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE mappings SET is_active = TRUE, version = version + 1, updated_at = now()
		 WHERE name = $1`, name); err != nil {
		return err
	}
	return nil
}

func activeInTx(ctx context.Context, tx pgx.Tx, name string) (*ActiveMapping, error) {
	row := tx.QueryRow(ctx,
		`SELECT name, version, spec_json FROM mappings WHERE name=$1`, name)
	var n, raw string
	var v uint32
	if err := row.Scan(&n, &v, &raw); err != nil {
		return nil, err
	}
	sp, err := mapping.ParseSpec([]byte(raw))
	if err != nil {
		return nil, err
	}
	return &ActiveMapping{Name: n, Version: v, Spec: sp}, nil
}

// AuditRecord is one conversion outcome persisted for observability.
type AuditRecord struct {
	Direction      string
	MappingName    string
	MappingVersion uint32
	RecordID       string
	OK             bool
	ErrorCode      string
	ErrorField     string
	CarriedEnum    *int32
}

func (s *Store) InsertAudit(ctx context.Context, r AuditRecord) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO conversion_audit
		  (direction, mapping_name, mapping_version, record_id, ok, error_code, error_field, carried_enum)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		r.Direction, r.MappingName, r.MappingVersion, r.RecordID, r.OK,
		r.ErrorCode, r.ErrorField, r.CarriedEnum)
	return err
}

// ListenMappings starts LISTEN mappings_changed. Changes are delivered on the
// returned channel (debounced) until ctx is canceled. Used for hot-switch
// fan-out across multiple gateway instances; the registry also polls.
func (s *Store) ListenMappings(ctx context.Context) (<-chan struct{}, error) {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	if _, err := conn.Exec(ctx, "LISTEN mappings_changed"); err != nil {
		conn.Release()
		return nil, err
	}
	ch := make(chan struct{}, 1)
	go func() {
		defer close(ch)
		defer conn.Release()
		for {
			_, err := conn.Conn().WaitForNotification(ctx)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				// Brief backoff on transient errors, then keep listening.
				select {
				case <-ctx.Done():
					return
				case <-time.After(time.Second):
				}
				continue
			}
			select {
			case ch <- struct{}{}:
			default:
			}
		}
	}()
	return ch, nil
}
