// Package telemetry defines the wire protocol of the telemetry channel and its
// parsing/validation rules.
//
// Topic:  telemetry/<deviceID>      (QoS 1, may be retained)
// Payload: UTF-8 JSON, see Message below.
//
// Validation failures are classified into two kinds:
//
//   - RejectError (permanent): malformed/unauthorized payload → quarantine row
//   - ACK, the message is dropped and never blocks other devices.
//   - RetryError (transient): payload is valid, but business processing could
//     not commit (e.g. injected failure) → no ACK; the MQTT broker redelivers.
package telemetry

import (
	"encoding/json"
	"errors"
	"fmt"
)

const TopicPrefix = "telemetry/"

// Message is one telemetry sample as sent by a device.
type Message struct {
	DeviceID string  `json:"device_id"`
	BootGen  int64   `json:"boot_gen"` // incremented every time the device process (re)starts
	Seq      int64   `json:"seq"`      // monotonic within (device_id, boot_gen), starting at 1
	Value    float64 `json:"value"`    // measured value (arbitrary unit)
	TSMillis int64   `json:"ts_ms"`    // measurement time, unix epoch milliseconds
	Sig      string  `json:"sig"`      // hex HMAC-SHA256 over the canonical string
}

// RejectError marks a payload as permanently invalid. It is acknowledged after
// being written to the quarantine table.
type RejectError struct{ Reason string }

func (e *RejectError) Error() string { return e.Reason }

// RetryError marks a valid message whose business transaction must be retried.
type RetryError struct{ Reason string }

func (e *RetryError) Error() string { return e.Reason }

func asReject(reason string) error { return &RejectError{Reason: reason} }

// AsRetry reports whether err is a RetryError.
func AsRetry(err error) bool {
	var r *RetryError
	return errors.As(err, &r)
}

// ParseAndValidate decodes raw JSON and performs all protocol-level checks that
// do not need the database. topicDeviceID is the device id extracted from the
// MQTT topic; the payload must agree with it.
func ParseAndValidate(topicDeviceID string, raw []byte) (*Message, error) {
	if len(raw) == 0 {
		return nil, asReject("empty payload")
	}
	var m Message
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, asReject("invalid JSON: " + err.Error())
	}
	if m.DeviceID == "" {
		return nil, asReject("missing device_id")
	}
	if topicDeviceID != "" && m.DeviceID != topicDeviceID {
		return nil, asReject(fmt.Sprintf("topic/device mismatch: topic=%q payload=%q", topicDeviceID, m.DeviceID))
	}
	if m.BootGen < 1 {
		return nil, asReject("boot_gen must be >= 1")
	}
	if m.Seq < 1 {
		return nil, asReject("seq must be >= 1")
	}
	if m.TSMillis <= 0 {
		return nil, asReject("ts_ms must be a positive unix millisecond timestamp")
	}
	if len(m.Sig) != 64 {
		return nil, asReject("sig must be 64 hex chars (HMAC-SHA256)")
	}
	return &m, nil
}
