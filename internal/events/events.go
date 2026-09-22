// Package events writes the append-only domain lifecycle audit log.
package events

import (
	"context"
	"database/sql"
	"encoding/json"

	"domainengine/internal/models"

	"github.com/jmoiron/sqlx"
)

// Insert appends an event. Callable inside a transaction. detail is optional.
// Auth codes must never appear in detail.
func Insert(ctx context.Context, tx sqlx.ExtContext, e models.DomainEvent) error {
	detail := e.Detail
	if len(detail) == 0 {
		detail = []byte("{}")
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO domain_events
			(domain_id, domain_name, event_type, from_status, to_status, amount_cents, detail)
		VALUES ($1,$2,$3,$4,$5,$6,$7)`,
		e.DomainID, e.DomainName, e.EventType, e.FromStatus, e.ToStatus,
		e.AmountCents, detail)
	return err
}

// ListForDomain returns a domain's audit trail, oldest first.
func ListForDomain(ctx context.Context, db *sqlx.DB, domainID int64) ([]models.DomainEvent, error) {
	var out []models.DomainEvent
	err := db.SelectContext(ctx, &out,
		`SELECT * FROM domain_events WHERE domain_id=$1 ORDER BY id`, domainID)
	return out, err
}

// ListRecent returns recent events across all domains (admin view).
func ListRecent(ctx context.Context, db *sqlx.DB, limit int) ([]models.DomainEvent, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var out []models.DomainEvent
	err := db.SelectContext(ctx, &out,
		`SELECT * FROM domain_events ORDER BY id DESC LIMIT $1`, limit)
	return out, err
}

// MustDetail JSON-encodes a detail map for Insert.
func MustDetail(m map[string]any) []byte {
	b, err := json.Marshal(m)
	if err != nil {
		return []byte("{}")
	}
	return b
}

var _ = sql.ErrNoRows
