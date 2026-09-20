// Package domain holds SIRCC lifecycle constants and shared errors.
package domain

import "errors"

// Incident statuses form a strict forward pipeline. No phase may be skipped.
const (
	StatusDetected   = "detected"
	StatusTriaged    = "triaged"
	StatusContained  = "contained"
	StatusEradicated = "eradicated"
	StatusRecovered  = "recovered"
	StatusReviewed   = "reviewed"
	StatusClosed     = "closed"
)

// Phase labels as persisted in incident_phases.
const (
	PhaseDetection   = "detection"
	PhaseTriage      = "triage"
	PhaseContainment = "containment"
	PhaseEradication = "eradication"
	PhaseRecovery    = "recovery"
	PhaseReview      = "review"
	PhaseClosure     = "closure"
)

// Roles.
const (
	RoleAdmin     = "admin"
	RoleAnalyst   = "analyst"
	RoleResponder = "responder"
)

const MaxEvidencePerIncident = 50

// AllStatuses is the ordered lifecycle.
var AllStatuses = []string{
	StatusDetected,
	StatusTriaged,
	StatusContained,
	StatusEradicated,
	StatusRecovered,
	StatusReviewed,
	StatusClosed,
}

// StatusToPhase maps an incident status to the phase entered when the
// incident reaches that status.
var StatusToPhase = map[string]string{
	StatusDetected:   PhaseDetection,
	StatusTriaged:    PhaseTriage,
	StatusContained:  PhaseContainment,
	StatusEradicated: PhaseEradication,
	StatusRecovered:  PhaseRecovery,
	StatusReviewed:   PhaseReview,
	StatusClosed:     PhaseClosure,
}

// NextStatus returns the only status reachable in one transition from s.
func NextStatus(s string) string {
	for i, cur := range AllStatuses {
		if cur == s && i+1 < len(AllStatuses) {
			return AllStatuses[i+1]
		}
	}
	return ""
}

// IsValidStatus reports whether s is a known lifecycle status.
func IsValidStatus(s string) bool {
	for _, cur := range AllStatuses {
		if cur == s {
			return true
		}
	}
	return false
}

// Domain errors. Handlers map these to HTTP status codes.
var (
	ErrNotFound          = errors.New("resource not found")
	ErrForbidden         = errors.New("operation not permitted for this role")
	ErrStaleVersion      = errors.New("expected version is out of date")
	ErrInvalidTransition = errors.New("invalid status transition")
	ErrClosureGate       = errors.New("closure requirements not met")
	ErrTriageGate        = errors.New("P1 incidents need an assigned responder before triage")
	ErrEvidenceCap       = errors.New("evidence limit reached for this incident")
	ErrValidation        = errors.New("request validation failed")
	ErrKeyReuse          = errors.New("idempotency key was already used for a different request")
)
