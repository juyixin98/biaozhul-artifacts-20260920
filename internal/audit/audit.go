// Package audit is the single entry point for writing audit log rows.
// Secrets must never be passed in detail — callers pass already-masked values.
package audit

import (
	"context"
	"encoding/json"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/store"
)

type Entry struct {
	ActorUser  *uuid.UUID
	ActorRole  string
	MerchantID *uuid.UUID
	Action     string
	TargetType string
	TargetID   string
	Masked     bool
	Detail     map[string]any
	IP         string
}

// Write enqueues the insert on the given transaction/connection (store.Querier
// accepts both pgx.Tx and *pgxpool.Pool). Failures are returned so the caller
// can abort the business transaction — audit loss must be visible.
func Write(ctx context.Context, q store.Querier, e Entry) error {
	var detail []byte
	var err error
	if e.Detail != nil {
		detail, err = json.Marshal(e.Detail)
	} else {
		detail = []byte("{}")
	}
	if err != nil {
		return err
	}
	return q.InsertAuditLog(ctx, store.InsertAuditLogParams{
		ActorUser:  e.ActorUser,
		ActorRole:  e.ActorRole,
		MerchantID: e.MerchantID,
		Action:     e.Action,
		TargetType: textOrNull(e.TargetType),
		TargetID:   textOrNull(e.TargetID),
		Masked:     e.Masked,
		Detail:     detail,
		Ip:         textOrNull(e.IP),
	})
}

func textOrNull(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}
