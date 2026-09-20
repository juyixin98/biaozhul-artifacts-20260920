package app

import "fmt"

// Error is an application error carrying an HTTP status code.
type Error struct {
	Status  int    `json:"-"`
	Message string `json:"error"`
}

func (e *Error) Error() string { return e.Message }

func newErr(status int, format string, args ...any) *Error {
	return &Error{Status: status, Message: fmt.Sprintf(format, args...)}
}

func BadRequestf(format string, args ...any) *Error { return newErr(400, format, args...) }

// NotFoundf is also used for cross-owner access: resources owned by someone
// else are invisible, not "forbidden", so existence is never leaked.
func NotFoundf(format string, args ...any) *Error { return newErr(404, format, args...) }
func Conflictf(format string, args ...any) *Error { return newErr(409, format, args...) }
