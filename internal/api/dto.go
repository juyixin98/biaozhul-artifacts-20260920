// Package api implements the HTTP transport.
package api

import (
	"encoding/json"
	"time"
)

// Envelope is the signed ingestion request. The signature covers the exact
// raw request body, method, path and timestamp, so clients must send the same
// bytes they signed.
type Envelope struct {
	DeviceID string   `json:"device_id"`
	Messages []MsgDTO `json:"messages"`
}

type MsgDTO struct {
	// Kind is "sample" (default when empty) or "heartbeat".
	Kind      string    `json:"kind,omitempty"`
	Seq       int64     `json:"seq,omitempty"`
	Value     string    `json:"value,omitempty"`
	SampledAt time.Time `json:"sampled_at"`
}

type registerRequest struct {
	ID   string `json:"id"`
	Type string `json:"type"`
}

type configRequest struct {
	DeviceType             string `json:"device_type"`
	StaleEnterTimeout      string `json:"stale_enter_timeout"`
	StaleRecoverTimeout    string `json:"stale_recover_timeout"`
	FrozenEnterCount       int    `json:"frozen_enter_count"`
	FrozenEnterMinDuration string `json:"frozen_enter_min_duration"`
	FrozenRecoverCount     int    `json:"frozen_recover_count"`
	MissingEnterCount      int    `json:"missing_enter_count"`
	MissingRecoverCount    int    `json:"missing_recover_count"`
	BackfillLookback       int64  `json:"backfill_lookback"`
}

type errorBody struct {
	Error string `json:"error"`
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}
