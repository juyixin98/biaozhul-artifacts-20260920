package core

import "fmt"

// ErrKind is the machine-readable category used for HTTP status mapping.
type ErrKind string

const (
	ErrValidation ErrKind = "validation_error"
	ErrNotFound   ErrKind = "not_found"
	ErrConflict   ErrKind = "conflict"
	ErrCycle      ErrKind = "cycle_detected"
	ErrState      ErrKind = "illegal_state"
	ErrNotHeld    ErrKind = "resource_not_held"
	ErrInternal   ErrKind = "internal_error"
)

// Error is the typed error carried across the service/API boundary.
type Error struct {
	Kind    ErrKind        `json:"kind"`
	Message string         `json:"message"`
	Details map[string]any `json:"details,omitempty"`
}

func (e *Error) Error() string {
	if len(e.Details) > 0 {
		return fmt.Sprintf("%s: %s (%v)", e.Kind, e.Message, e.Details)
	}
	return fmt.Sprintf("%s: %s", e.Kind, e.Message)
}

func errf(kind ErrKind, format string, args ...any) *Error {
	return &Error{Kind: kind, Message: fmt.Sprintf(format, args...)}
}

// Errorf builds a typed core error (used by the API layer for input errors).
func Errorf(kind string, format string, args ...any) *Error {
	return &Error{Kind: ErrKind(kind), Message: fmt.Sprintf(format, args...)}
}

func errDetail(kind ErrKind, msg string, details map[string]any) *Error {
	return &Error{Kind: kind, Message: msg, Details: details}
}
