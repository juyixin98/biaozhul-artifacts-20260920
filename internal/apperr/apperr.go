// Package apperr defines domain errors shared by the service layer.
// The HTTP layer maps them to status codes; services never touch net/http.
package apperr

import "errors"

type Error struct {
	Status int
	Code   string
	Msg    string
}

func (e *Error) Error() string { return e.Code + ": " + e.Msg }

func New(status int, code, msg string) *Error {
	return &Error{Status: status, Code: code, Msg: msg}
}

var (
	ErrNotFound        = New(404, "not_found", "resource not found")
	ErrUnauthorized    = New(401, "unauthorized", "authentication required or invalid token")
	ErrForbidden       = New(403, "forbidden", "you may not perform this action")
	ErrConflict        = New(409, "conflict", "action conflicts with the current state")
	ErrValidation      = New(422, "validation_error", "request failed validation")
	ErrDuplicateReport = New(409, "duplicate_report", "you have already reported this content")
	ErrNoTask          = New(409, "no_task", "no review task available")
	ErrStaleClaim      = New(409, "stale_claim", "your claim has expired or the revision changed")
	ErrSensitiveWords  = New(422, "sensitive_words", "body contains forbidden words")
)

func As(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}
