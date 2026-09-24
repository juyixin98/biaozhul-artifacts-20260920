// Package evidence implements the tamper-evident audit ledger.
//
// Every allocation decision is rendered into a canonical string and signed
// with HMAC-SHA256 over a service-held secret key. The signature is real
// cryptography (crypto/hmac + crypto/sha256), compared with hmac.Equal on
// verification, and the canonical payload is built deterministically so that
// changing any single field invalidates the signature.
package evidence

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
)

// CanonicalVersion is bumped whenever the canonical payload format changes.
const CanonicalVersion = "dlc-ev1"

// Signer signs and verifies audit events.
type Signer struct {
	key []byte
}

// NewSigner builds a signer from raw key bytes (16+ bytes recommended).
func NewSigner(key []byte) *Signer {
	cp := make([]byte, len(key))
	copy(cp, key)
	return &Signer{key: cp}
}

// GenerateKey returns a fresh 256-bit random key, base64-std encoded for
// storage in service_meta.
func GenerateKey() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generate evidence key: %w", err)
	}
	return base64.StdEncoding.EncodeToString(buf), nil
}

// ParseKey accepts a stored base64 key, or falls back to using the raw
// string bytes when EVIDENCE_SECRET was provided as plain text.
func ParseKey(stored string) ([]byte, error) {
	if raw, err := base64.StdEncoding.DecodeString(stored); err == nil && len(raw) >= 16 {
		return raw, nil
	}
	return []byte(stored), nil
}

// Resource is one claimed resource, rendered canonically as "kind/name".
type Resource struct {
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// Event is the payload covered by the signature.
type Event struct {
	AuditID   int64      `json:"audit_id"`
	At        time.Time  `json:"at"`
	Event     string     `json:"event"`
	TaskID    *int64     `json:"task_id"`
	RequestID *int64     `json:"request_id"`
	Resources []Resource `json:"resources"`
}

// Canonical produces the deterministic string that is signed.
//
// Format (newline separated):
//
//	dlc-ev1
//	audit_id=<id>
//	at=<RFC3339Nano UTC>
//	event=<event>
//	task_id=<n|->
//	request_id=<n|->
//	resources=<kind/name sorted, comma joined>
func Canonical(e Event) string {
	rs := make([]string, len(e.Resources))
	for i, r := range e.Resources {
		rs[i] = r.Kind + "/" + r.Name
	}
	sort.Strings(rs)

	tid := "-"
	if e.TaskID != nil {
		tid = strconv.FormatInt(*e.TaskID, 10)
	}
	rid := "-"
	if e.RequestID != nil {
		rid = strconv.FormatInt(*e.RequestID, 10)
	}

	return strings.Join([]string{
		CanonicalVersion,
		"audit_id=" + strconv.FormatInt(e.AuditID, 10),
		"at=" + e.At.UTC().Format(time.RFC3339Nano),
		"event=" + e.Event,
		"task_id=" + tid,
		"request_id=" + rid,
		"resources=" + strings.Join(rs, ","),
	}, "\n")
}

// Sign returns the lowercase hex HMAC-SHA256 of the canonical payload.
func (s *Signer) Sign(canonical string) string {
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(canonical))
	return hex.EncodeToString(mac.Sum(nil))
}

// Verify recomputes the signature in constant time.
func (s *Signer) Verify(canonical, sig string) bool {
	got, err := hex.DecodeString(sig)
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(canonical))
	return hmac.Equal(got, mac.Sum(nil))
}

// ResourcesJSON is the canonical JSON encoding stored in audit_events.resources.
func ResourcesJSON(rs []Resource) ([]byte, error) {
	if rs == nil {
		rs = []Resource{}
	}
	out := make([]Resource, len(rs))
	copy(out, rs)
	sort.Slice(out, func(i, j int) bool {
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].Name < out[j].Name
	})
	b, err := json.Marshal(out)
	if err != nil {
		return nil, fmt.Errorf("encode resources: %w", err)
	}
	return b, nil
}
