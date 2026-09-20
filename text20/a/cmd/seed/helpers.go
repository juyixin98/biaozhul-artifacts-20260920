package main

import (
	"crypto/sha256"
	"encoding/hex"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

func toInt8(p *int64) pgtype.Int8 {
	if p == nil {
		return pgtype.Int8{Valid: false}
	}
	return pgtype.Int8{Int64: *p, Valid: true}
}

func toTS(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

func sha256hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}
