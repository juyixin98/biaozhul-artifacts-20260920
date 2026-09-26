// Package fault implements the in-process fake external dependency: a
// thread-safe, scripted sequence of outcomes consumed one per attempt.
// Scripts replace all real downstream systems in this project.
package fault

import (
	"sync"
	"time"
)

// Outcome is one scripted response of the fake dependency.
type Outcome struct {
	// Status is the synthetic HTTP status the fake "returns"
	// (e.g. 200, 429, 503, 400).
	Status int `json:"status"`
	// RetryAfter, when positive, is advertised as a server-side wait hint.
	RetryAfter time.Duration `json:"retry_after_ms"`
	// Message is attached to the synthetic response body.
	Message string `json:"message"`
}

// Script is an ordered outcome list keyed per request ID so concurrent
// requests never share a cursor. When the list is exhausted the final
// outcome repeats forever.
type Script struct {
	mu       sync.Mutex
	outcomes []Outcome
	cursor   map[string]int
	hits     map[string]int
}

// NewScript builds a script. An empty script succeeds with 200.
func NewScript(outcomes ...Outcome) *Script {
	return &Script{
		outcomes: append([]Outcome(nil), outcomes...),
		cursor:   map[string]int{},
		hits:     map[string]int{},
	}
}

// Set replaces the scripted outcomes atomically (used by the demo control
// endpoint between requests).
func (s *Script) Set(outcomes ...Outcome) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.outcomes = append([]Outcome(nil), outcomes...)
	s.cursor = map[string]int{}
	s.hits = map[string]int{}
}

// Next returns the outcome for the given request's next attempt and the
// 1-based hit number.
func (s *Script) Next(requestID string) (Outcome, int) {
	s.mu.Lock()
	defer s.mu.Unlock()

	pos := s.cursor[requestID]
	s.hits[requestID]++
	hits := s.hits[requestID]

	if len(s.outcomes) == 0 {
		return Outcome{Status: 200, Message: "ok (empty script)"}, hits
	}
	if pos >= len(s.outcomes) {
		pos = len(s.outcomes) - 1
	}
	out := s.outcomes[pos]
	s.cursor[requestID] = pos + 1
	return out, hits
}

// Hits reports how many times the fake was invoked for requestID.
func (s *Script) Hits(requestID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.hits[requestID]
}

// Common outcome constructors for readability in fixtures.

func OK() Outcome { return Outcome{Status: 200, Message: "ok"} }

func Unavailable(retryAfter time.Duration) Outcome {
	return Outcome{Status: 503, RetryAfter: retryAfter, Message: "service unavailable"}
}

func TooManyRequests(retryAfter time.Duration) Outcome {
	return Outcome{Status: 429, RetryAfter: retryAfter, Message: "rate limited"}
}

func BadRequest() Outcome {
	return Outcome{Status: 400, Message: "deterministic client error"}
}
