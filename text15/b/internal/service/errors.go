package service

import "fmt"

// Error is a coded service error mapped to an HTTP status by the handlers.
type Error struct {
	Status  int
	Code    string
	Message string
	Details any
}

func (e *Error) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

// withDetail attaches structured detail (e.g. which items block sign-off).
func (e *Error) withDetail(d any) *Error {
	e.Details = d
	return e
}

func errf(status int, code, format string, args ...any) *Error {
	return &Error{Status: status, Code: code, Message: fmt.Sprintf(format, args...)}
}

func errBadRequest(msg string) *Error {
	return &Error{Status: 400, Code: "bad_request", Message: msg}
}

func errUnauthorized(msg string) *Error {
	return &Error{Status: 401, Code: "unauthorized", Message: msg}
}

func errForbidden(msg string) *Error {
	return &Error{Status: 403, Code: "forbidden", Message: msg}
}

func errNotFound(msg string) *Error {
	return &Error{Status: 404, Code: "not_found", Message: msg}
}

func errConflict(msg string) *Error {
	return &Error{Status: 409, Code: "conflict", Message: msg}
}

// errUnprocessable is the business-rule rejection (422): failed/pending items
// block approval, superseded versions, invalid verdict payloads, etc.
func errUnprocessable(code, msg string) *Error {
	return &Error{Status: 422, Code: code, Message: msg}
}

const httpStatusInternal = 500
