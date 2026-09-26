// Package report defines the structured result format emitted by the
// self-test runner. Results are immutable snapshots: a Case is built once
// and never mutated; the Suite accumulates them by copy.
package report

import (
	"encoding/json"
	"io"
	"time"
)

// Status values for a case.
const (
	StatusPass = "pass"
	StatusFail = "fail"
	StatusSkip = "skip"
)

// RequestSpec records the wire request that was sent.
type RequestSpec struct {
	Method  string            `json:"method"`
	Path    string            `json:"path"`
	Headers map[string]string `json:"headers,omitempty"`
}

// ResponseSpec records the observed wire response essentials.
type ResponseSpec struct {
	StatusCode    int               `json:"status_code"`
	Headers       map[string]string `json:"headers,omitempty"`
	ContentLength int64             `json:"content_length"`
	BodySHA256    string            `json:"body_sha256,omitempty"`
	BodyLength    int               `json:"body_length,omitempty"`
}

// Case is one acceptance scenario result.
type Case struct {
	ID        string       `json:"id"`
	Name      string       `json:"name"`
	Given     string       `json:"given"`
	When      string       `json:"when"`
	Then      string       `json:"then"`
	Status    string       `json:"status"`
	Detail    string       `json:"detail,omitempty"`
	Request   RequestSpec  `json:"request"`
	Response  ResponseSpec `json:"response"`
	ElapsedMS int64        `json:"elapsed_ms"`
}

// Suite is the full run.
type Suite struct {
	Name       string    `json:"name"`
	StartedAt  time.Time `json:"started_at"`
	DurationMS int64     `json:"duration_ms"`
	Cases      []Case    `json:"cases"`
}

// Summary returns (pass, fail, skip) counts.
func (s Suite) Summary() (pass, fail, skip int) {
	for _, c := range s.Cases {
		switch c.Status {
		case StatusPass:
			pass++
		case StatusFail:
			fail++
		case StatusSkip:
			skip++
		}
	}
	return pass, fail, skip
}

// WriteJSON emits the suite as indented JSON.
func WriteJSON(w io.Writer, s Suite) error {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(s)
}
