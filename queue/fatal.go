package queue

import "errors"

// FatalError marks an execution error that must not be retried. Executor
// implementations wrap errors with Fatal; the scheduler checks with IsFatal.
type FatalError struct{ Err error }

func (e *FatalError) Error() string { return "fatal: " + e.Err.Error() }
func (e *FatalError) Unwrap() error { return e.Err }

// Fatal wraps err so the scheduler does not retry the job. A nil err yields nil.
func Fatal(err error) error {
	if err == nil {
		return nil
	}
	return &FatalError{Err: err}
}

// IsFatal reports whether err (or anything in its chain) is a *FatalError.
func IsFatal(err error) bool {
	var fe *FatalError
	return errors.As(err, &fe)
}
