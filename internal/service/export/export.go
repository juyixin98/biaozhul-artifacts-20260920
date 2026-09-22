// Package export renders events with sensitive fields masked. The same mask
// applies regardless of role — even admins and auditors receive masked
// payloads through the export surface; raw sql_text/client_ip never leave
// the service via export.
package export

import (
	"dams/internal/db"
)

const masked = "***MASKED***"

// MaskedEvent is the redacted projection of an event.
type MaskedEvent struct {
	ID             int64  `json:"id"`
	SourceID       int64  `json:"source_id"`
	SourceEventID  string `json:"source_event_id"`
	DbUser         string `json:"db_user"`
	OccurredAt     string `json:"occurred_at"`
	ActionCategory string `json:"action_category"`
	SchemaName     string `json:"schema_name"`
	TableName      string `json:"table_name"`
	RowCount       int64  `json:"row_count"`
	ClientIP       string `json:"client_ip"`
	SQLText        string `json:"sql_text"`
}

// Event masks one row.
func Event(e db.Event) MaskedEvent {
	return MaskedEvent{
		ID:             e.ID,
		SourceID:       e.SourceID,
		SourceEventID:  e.SourceEventID,
		DbUser:         e.DbUser,
		OccurredAt:     e.OccurredAt.Time.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"),
		ActionCategory: e.ActionCategory,
		SchemaName:     e.SchemaName,
		TableName:      e.TableName,
		RowCount:       e.RowCount,
		ClientIP:       maskIfPresent(e.ClientIp),
		SQLText:        maskIfPresent(e.SqlText),
	}
}

// EventMap is used where handlers render dynamic maps.
func EventMap(e db.Event) map[string]any {
	return map[string]any{
		"id":              e.ID,
		"source_id":       e.SourceID,
		"source_event_id": e.SourceEventID,
		"db_user":         e.DbUser,
		"occurred_at":     e.OccurredAt.Time,
		"action_category": e.ActionCategory,
		"schema_name":     e.SchemaName,
		"table_name":      e.TableName,
		"row_count":       e.RowCount,
		"client_ip":       maskIfPresent(e.ClientIp),
		"sql_text":        maskIfPresent(e.SqlText),
	}
}

func maskIfPresent(v string) string {
	if v == "" {
		return ""
	}
	return masked
}
