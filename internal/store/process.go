// Package store implements the transactional persistence boundary.
//
// The rule enforced here is the heart of the system:
//
//	A QoS 1 PUBLISH is MQTT-acknowledged only after one Postgres transaction
//	has durably committed everything required for that delivery
//	(raw message + business state + dedup key).
//
// If the transaction rolls back, nothing is ACKed and MQTT redelivers the
// message; reprocessing is idempotent thanks to the business primary key.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schemaSQL string

// tx is a test-facing alias for the transaction handle.
type tx = pgx.Tx

// Store wraps a Postgres connection pool.
type Store struct {
	pool *pgxpool.Pool
}

// Open creates the pool and applies schema migrations (idempotent).
// It retries while PostgreSQL is accepting TCP but still starting up
// (SQLSTATE 57P03), which pg_isready alone does not fully serialize.
func Open(ctx context.Context, dsn string) error {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return fmt.Errorf("connect postgres: %w", err)
	}

	deadline := time.Now().Add(30 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		if err := pool.Ping(ctx); err == nil {
			lastErr = nil
			break
		} else {
			lastErr = err
			select {
			case <-ctx.Done():
				pool.Close()
				return ctx.Err()
			case <-time.After(500 * time.Millisecond):
			}
		}
	}
	if lastErr != nil {
		pool.Close()
		return fmt.Errorf("ping postgres: %w", lastErr)
	}
	if _, err := pool.Exec(ctx, schemaSQL); err != nil {
		pool.Close()
		return fmt.Errorf("apply schema: %w", err)
	}
	globalPool = pool
	return nil
}

var globalPool *pgxpool.Pool

// DB returns the initialized pool.
func DB() *pgxpool.Pool { return globalPool }

// Close releases the pool.
func Close() {
	if globalPool != nil {
		globalPool.Close()
	}
}

// InTx runs fn inside a single transaction, committing on nil error and
// rolling back otherwise.
func InTx(ctx context.Context, fn func(pgx.Tx) error) error {
	tx, err := globalPool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err := fn(tx); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit: %w", err)
	}
	return nil
}

// EventInput carries one validated telemetry event into the transaction.
type EventInput struct {
	DeviceID   string
	BootGen    int64
	Seq        int64
	Value      float64
	MeasuredAt time.Time
	Topic      string
	PacketID   uint16
	DupFlag    bool
	PayloadRaw []byte
}

// EventResult tells the caller what kind of processing happened.
type EventResult string

const (
	// ResultInserted: new business event committed.
	ResultInserted EventResult = "inserted"
	// ResultDuplicate: redelivery of an already committed event.
	ResultDuplicate EventResult = "duplicate"
)

// ProcessEvent is the atomic "dedup + business state + raw audit" operation.
// It MUST run inside store.InTx.
//
// Order inside the transaction:
//  1. Register/refresh the boot generation (row lock orders concurrent
//     deliveries of the same device).
//  2. INSERT into events — the PRIMARY KEY (device_id, boot_gen, seq) is the
//     business dedup key. A unique violation means this delivery is a
//     transport-level duplicate of an event already committed.
//  3. Only for new events: boot counters + online state. The state moves
//     forward exclusively for the newest boot (device_boots_is_newer), so a
//     device restart never mixes up seq spaces.
//  4. Raw transport bytes are audited in the same transaction
//     (kind='event' / 'dup').
//  5. failDevice simulates a business failure: all of the above rolls back.
func ProcessEvent(ctx context.Context, tx pgx.Tx, in EventInput, failDevice string) (EventResult, error) {
	// 1) Ensure the boot row exists and take a row lock on it. This serializes
	//    two concurrent deliveries (original + fast redelivery) for the same
	//    (device, boot) without relying on app-side locking.
	if _, err := tx.Exec(ctx, `
		INSERT INTO device_boots (device_id, boot_gen)
		VALUES ($1,$2)
		ON CONFLICT DO NOTHING
	`, in.DeviceID, in.BootGen); err != nil {
		return "", fmt.Errorf("register boot: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		SELECT 1 FROM device_boots
		WHERE device_id=$1 AND boot_gen=$2
		FOR UPDATE
	`, in.DeviceID, in.BootGen); err != nil {
		return "", fmt.Errorf("lock boot: %w", err)
	}

	// 2) Business dedup via primary key. ON CONFLICT DO NOTHING + RETURNING:
	//    a duplicate yields zero rows WITHOUT raising an error. This matters —
	//    in PostgreSQL, after ANY statement error the whole transaction is
	//    aborted (SQLSTATE 25P02), so catching a unique violation and carrying
	//    on is not possible without a savepoint. Conflict-avoiding SQL is the
	//    correct, abort-free way to branch on dedup.
	var inserted bool
	if err := tx.QueryRow(ctx, `
		INSERT INTO events (device_id, boot_gen, seq, value, measured_at)
		VALUES ($1,$2,$3,$4,$5)
		ON CONFLICT (device_id, boot_gen, seq) DO NOTHING
		RETURNING true
	`, in.DeviceID, in.BootGen, in.Seq, in.Value, in.MeasuredAt).Scan(&inserted); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			inserted = false // duplicate delivery of an existing event
		} else {
			return "", fmt.Errorf("insert event: %w", err)
		}
	}

	kind := "dup"
	if inserted {
		kind = "event"

		// 3a) Boot counters.
		if _, err := tx.Exec(ctx, `
			UPDATE device_boots
			SET last_seq    = GREATEST(last_seq, $3),
			    event_count = event_count + 1
			WHERE device_id=$1 AND boot_gen=$2
		`, in.DeviceID, in.BootGen, in.Seq); err != nil {
			return "", fmt.Errorf("update boot counters: %w", err)
		}

		// 3b) Online state in ONE abort-free upsert.
		//     - No state row yet (brand-new device) → INSERT.
		//     - Existing row at the same/newer boot → UPDATE only when the
		//       incoming boot is >= current; the WHERE makes a strictly older
		//       boot (late packet after restart) a no-op that never rewinds.
		if _, err := tx.Exec(ctx, `
			INSERT INTO device_state
				(device_id, current_boot_gen, current_seq, last_value, last_event_at)
			VALUES ($1,$2,$3,$4,$5)
			ON CONFLICT (device_id) DO UPDATE
			SET current_boot_gen = EXCLUDED.current_boot_gen,
			    current_seq      = EXCLUDED.current_seq,
			    last_value       = EXCLUDED.last_value,
			    last_event_at    = EXCLUDED.last_event_at,
			    updated_at       = now()
			WHERE EXCLUDED.current_boot_gen >= device_state.current_boot_gen
		`, in.DeviceID, in.BootGen, in.Seq, in.Value, in.MeasuredAt); err != nil {
			return "", fmt.Errorf("upsert state: %w", err)
		}
	}

	// 4) Raw transport audit in the SAME transaction.
	if _, err := tx.Exec(ctx, `
		INSERT INTO raw_messages
			(device_id, mqtt_topic, packet_id, dup_flag, retained_flag, payload, payload_raw, kind)
		VALUES ($1,$2,$3,$4,$5, $6, $7, $8)
	`, in.DeviceID, in.Topic, nilIfZero(in.PacketID), in.DupFlag, false,
		jsonOrNull(in.PayloadRaw), in.PayloadRaw, kind); err != nil {
		return "", fmt.Errorf("insert raw message: %w", err)
	}

	// 5) Injected business failure: caller must NOT ACK → MQTT redelivers.
	if failDevice != "" && failDevice == in.DeviceID {
		return "", fmt.Errorf("injected business failure for device %q", failDevice)
	}

	if inserted {
		return ResultInserted, nil
	}
	return ResultDuplicate, nil
}

func nilIfZero(id uint16) any {
	if id == 0 {
		return nil
	}
	return id
}

// RecordSnapshot records a RETAINED message. Retained payloads are startup
// snapshots only: they are audited but NEVER create events and NEVER refresh
// online state. Committed in its own short transaction; then ACKed.
func RecordSnapshot(ctx context.Context, deviceHint, topic string, packetID uint16, payloadRaw []byte) error {
	return InTx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `
			INSERT INTO raw_messages
				(device_id, mqtt_topic, packet_id, dup_flag, retained_flag, payload, payload_raw, kind)
			VALUES ($1,$2,$3,false,true,$4,$5,'snapshot')
		`, nullableDevice(deviceHint), topic, nilIfZero(packetID), jsonOrNull(payloadRaw), payloadRaw)
		if err != nil {
			return fmt.Errorf("insert snapshot: %w", err)
		}
		return nil
	})
}

// RecordQuarantine persists an invalid payload with the rejection reason.
// It is ACKed afterwards so one bad device cannot block the broker queue.
func RecordQuarantine(ctx context.Context, deviceHint, topic string, packetID uint16, dup, retained bool, payloadRaw []byte, reason string) error {
	return InTx(ctx, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `
			INSERT INTO raw_messages
				(device_id, mqtt_topic, packet_id, dup_flag, retained_flag, payload, payload_raw, kind)
			VALUES ($1,$2,$3,$4,$5,$6,$7,'quarantine')
		`, nullableDevice(deviceHint), topic, nilIfZero(packetID), dup, retained,
			jsonOrNull(payloadRaw), payloadRaw); err != nil {
			return fmt.Errorf("insert raw(quarantine): %w", err)
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO quarantine (device_hint, mqtt_topic, payload_raw, reason)
			VALUES ($1,$2,$3,$4)
		`, nullableDevice(deviceHint), topic, payloadRaw, reason); err != nil {
			return fmt.Errorf("insert quarantine: %w", err)
		}
		return nil
	})
}

func nullableDevice(d string) any {
	if d == "" {
		return nil
	}
	return d
}

// jsonOrNull returns a value pgx encodes as the json/jsonb type, or nil.
// json.RawMessage is registered by pgx v5 with the json OID (a plain []byte
// would be encoded as bytea, which has no implicit cast to jsonb).
func jsonOrNull(raw []byte) any {
	if len(raw) > 0 && json.Valid(raw) {
		return json.RawMessage(raw)
	}
	return nil
}
