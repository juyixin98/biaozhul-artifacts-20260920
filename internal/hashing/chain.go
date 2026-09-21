// Package hashing builds the canonical content and digest links of the
// append-only evidence chain.
package hashing

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// CanonicalVersion tags the serialization format. Any incompatible change to
// canonicalization must bump this value so historical links stay verifiable.
const CanonicalVersion = 1

const genesisPrefix = "forensiccore-genesis-v1:"

// GenesisHash derives the anchor digest for a case.
func GenesisHash(caseID string) string {
	sum := sha256.Sum256([]byte(genesisPrefix + caseID))
	return hex.EncodeToString(sum[:])
}

// EventContent is the deterministic envelope stored as ChainEvent.ContentJSON.
// Field order is fixed and no maps are used, so encoding/json output is
// reproducible byte-for-byte across machines.
type EventContent struct {
	Version   int             `json:"v"`
	CaseID    string          `json:"case_id"`
	EventType string          `json:"event_type"`
	Actor     string          `json:"actor"`
	Timestamp string          `json:"ts"`
	Payload   json.RawMessage `json:"payload"`
}

// CanonicalJSON marshals the envelope. Timestamp is normalized to RFC3339 with
// nanosecond precision in UTC. The payload is first unmarshalled into generic
// values and re-encoded with sorted keys, so canonical bytes never depend on
// Go struct field order or on how callers built the value.
func CanonicalJSON(caseID, eventType, actor string, ts time.Time, payload any) (string, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("canonicalize payload: %w", err)
	}
	var generic any
	if err := json.Unmarshal(raw, &generic); err != nil {
		return "", fmt.Errorf("canonicalize payload: %w", err)
	}
	canonicalPayload, err := marshalSorted(generic)
	if err != nil {
		return "", fmt.Errorf("canonicalize payload: %w", err)
	}
	env := EventContent{
		Version:   CanonicalVersion,
		CaseID:    caseID,
		EventType: eventType,
		Actor:     actor,
		Timestamp: ts.UTC().Format(time.RFC3339Nano),
		Payload:   canonicalPayload,
	}
	out, err := json.Marshal(env)
	if err != nil {
		return "", fmt.Errorf("canonicalize envelope: %w", err)
	}
	return string(out), nil
}

// marshalSorted re-encodes decoded JSON with object keys sorted recursively.
func marshalSorted(v any) (json.RawMessage, error) {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sortStrings(keys)
		var sb strings.Builder
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteByte(',')
			}
			kb, err := json.Marshal(k)
			if err != nil {
				return nil, err
			}
			sb.Write(kb)
			sb.WriteByte(':')
			vb, err := marshalSorted(t[k])
			if err != nil {
				return nil, err
			}
			sb.Write(vb)
		}
		sb.WriteByte('}')
		return json.RawMessage(sb.String()), nil
	case []any:
		var sb strings.Builder
		sb.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				sb.WriteByte(',')
			}
			eb, err := marshalSorted(e)
			if err != nil {
				return nil, err
			}
			sb.Write(eb)
		}
		sb.WriteByte(']')
		return json.RawMessage(sb.String()), nil
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return nil, err
		}
		return json.RawMessage(b), nil
	}
}

func sortStrings(s []string) {
	// Small dependency-free insertion sort; payload objects are tiny.
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}

// LinkDigest computes SHA-256(prevDigest || contentJSON).
func LinkDigest(prevDigest, contentJSON string) string {
	h := sha256.New()
	h.Write([]byte(prevDigest))
	h.Write([]byte(contentJSON))
	return hex.EncodeToString(h.Sum(nil))
}

// SHA256Hex returns the lowercase hex SHA-256 of b.
func SHA256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Payload structs. Their JSON tags and field order are part of the canonical
// contract — reordering or renaming fields breaks historical chain links.

type CaseCreatedPayload struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

type RegisteredPayload struct {
	EvidenceID string `json:"evidence_id"`
	SourcePath string `json:"source_path"`
	RealPath   string `json:"real_path"`
	Filename   string `json:"filename"`
	Size       int64  `json:"size"`
	SHA256     string `json:"sha256"`
}

type ReviewPayload struct {
	JobID          string `json:"job_id"`
	EvidenceID     string `json:"evidence_id"`
	Status         string `json:"status"`
	FinalSHA256    string `json:"final_sha256"`
	BaselineSHA256 string `json:"baseline_sha256"`
	Match          bool   `json:"match"`
	Result         string `json:"result"`
}

type TransferPayload struct {
	EvidenceID    string `json:"evidence_id"`
	FromCustodian string `json:"from_custodian"`
	ToCustodian   string `json:"to_custodian"`
	Reason        string `json:"reason"`
}

type NotePayload struct {
	EvidenceID string `json:"evidence_id"`
	Note       string `json:"note"`
}
