// Package app contains the SynapticGo business logic: chunked dataset
// ingest with crash-safe merge, content-addressed file storage with
// reference-counted garbage collection, immutable model versions, real CPU
// inference and experiment records.
package app

import "fmt"

// Error is a domain error carrying the HTTP status it should render with.
type Error struct {
	Status  int    `json:"-"`
	Message string `json:"error"`
}

func (e *Error) Error() string { return e.Message }

func newErr(status int, format string, args ...any) *Error {
	return &Error{Status: status, Message: fmt.Sprintf(format, args...)}
}

func BadRequestf(format string, args ...any) *Error { return newErr(400, format, args...) }

// NotFoundf doubles as the cross-owner response: a resource owned by another
// user is reported as not found rather than forbidden, so its existence is
// never leaked.
func NotFoundf(format string, args ...any) *Error { return newErr(404, format, args...) }
func Conflictf(format string, args ...any) *Error { return newErr(409, format, args...) }
