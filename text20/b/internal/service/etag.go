package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// etagState is the state-bearing subset of ScreenMenu. GeneratedAt is
// deliberately excluded: two requests at different wall-clock times but the
// same published version, temporary prices and sellout flags must produce
// the same ETag, while any of those changes must produce a different one.
type etagState struct {
	StoreID     int64            `json:"store_id"`
	Version     int64            `json:"version"`
	PublishedAt string           `json:"published_at"`
	Lines       []ScreenMenuLine `json:"lines"`
}

// ETag returns a strong validator for the rendered menu state.
// It is safe to use with If-None-Match and If-Match.
func (m ScreenMenu) ETag() string {
	st := etagState{
		StoreID:     m.StoreID,
		Version:     m.Version,
		PublishedAt: m.PublishedAt.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"),
		Lines:       m.Lines,
	}
	b, _ := json.Marshal(st) // all fields are trivially marshalable
	sum := sha256.Sum256(b)
	return `"` + hex.EncodeToString(sum[:]) + `"`
}
