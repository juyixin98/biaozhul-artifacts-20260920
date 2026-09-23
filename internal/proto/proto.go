// Package proto defines the wire payloads exchanged between client nodes, the
// lock service and the protected resource in the simulated network.
//
// Messages are carried inside sim.Message.Body maps (encoding/json values), so
// every payload has (de)serialization helpers. Using typed structs keeps the
// three services honest about their contract while the simulator stays
// type-agnostic.
package proto

import (
	"encoding/json"
	"fmt"
)

// Lock methods (node -> lock service, and lock service -> node).
const (
	Acquire     = "lock.acquire"
	AcquireResp = "lock.acquire.resp"
	Renew       = "lock.renew"
	RenewResp   = "lock.renew.resp"
	Release     = "lock.release"
	ReleaseResp = "lock.release.resp"
)

// Resource methods (node -> resource, resource -> node).
const (
	Submit      = "resource.submit"
	SubmitReply = "resource.submit.resp"
)

// AcquireReq requests exclusive access to Resource. If the caller already
// holds the fence for Resource it is a re-entrant refresh and the same fence
// token is returned.
type AcquireReq struct {
	Resource string `json:"resource"`
	TTL      int64  `json:"ttl"` // requested lease length in ticks
	ReqID    string `json:"req_id"`
}

// RenewReq extends the lease associated with Fence.
type RenewReq struct {
	Resource string `json:"resource"`
	Fence    int64  `json:"fence"`
	TTL      int64  `json:"ttl"`
	ReqID    string `json:"req_id"`
}

// ReleaseReq voluntarily gives the lock back.
type ReleaseReq struct {
	Resource string `json:"resource"`
	Fence    int64  `json:"fence"`
	ReqID    string `json:"req_id"`
}

// LockResp is the reply to all three lock methods.
type LockResp struct {
	OK       bool   `json:"ok"`
	Fence    int64  `json:"fence"`
	Resource string `json:"resource"`
	Expiry   int64  `json:"expiry"` // absolute simulated time
	Reason   string `json:"reason,omitempty"`
	ReqID    string `json:"req_id"`
}

// SubmitReq asks the resource to append Value to its log on behalf of the
// fence token holder.
type SubmitReq struct {
	Resource string `json:"resource"`
	Fence    int64  `json:"fence"`
	Value    string `json:"value"`
	ReqID    string `json:"req_id"`
}

// SubmitResp reports the resource's decision.
type SubmitResp struct {
	OK        bool   `json:"ok"`
	Accepted  bool   `json:"accepted"` // redundant with OK, kept explicit for logs
	Fence     int64  `json:"fence"`
	HighWater int64  `json:"high_water"`
	Reason    string `json:"reason,omitempty"`
	Value     string `json:"value"`
	ReqID     string `json:"req_id"`
}

// Encode converts a typed payload to a body map.
func Encode(v any) map[string]any {
	raw, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("proto: encode: %v", err))
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		panic(fmt.Sprintf("proto: encode: %v", err))
	}
	return m
}

// Decode fills a typed payload from a body map.
func Decode(body map[string]any, v any) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(raw, v); err != nil {
		return fmt.Errorf("proto: decode: %w", err)
	}
	return nil
}
