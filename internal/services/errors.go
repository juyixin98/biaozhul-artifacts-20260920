package services

import (
	"errors"
	"net/http"
)

// Sentinel errors mapped to HTTP status codes at the handler boundary.
var (
	ErrNotFound        = errors.New("not found")
	ErrConflict        = errors.New("conflict")
	ErrForbidden       = errors.New("forbidden")
	ErrUnauthorized    = errors.New("unauthorized")
	ErrValidation      = errors.New("validation error")
	ErrSelfReview      = errors.New("authors cannot review their own posts")
	ErrReviewerRenewal = errors.New("reviewers cannot record payments or renew memberships")
	ErrTierLimit       = errors.New("a community can define at most 10 tiers")
	ErrMaxTiers        = errors.New("tier level must be between 1 and 10")
	ErrAppealUsed      = errors.New("a report can be appealed at most once")
	ErrReportState     = errors.New("illegal report transition")
	ErrPostState       = errors.New("illegal post transition in current state")
	ErrVersionMismatch = errors.New("post was modified after review was requested: re-open the latest version")
	ErrNewerVersion    = errors.New("a newer version exists: old version restore would clobber later changes")
	ErrCourseState     = errors.New("only draft courses can be edited")
	ErrPublishContent  = errors.New("course content failed publication validation")
)

// Error pairs a sentinel with a human-readable detail.
type Error struct {
	Kind   error
	Detail string
}

func (e *Error) Error() string {
	if e.Detail != "" {
		return e.Kind.Error() + ": " + e.Detail
	}
	return e.Kind.Error()
}

func (e *Error) Unwrap() error { return e.Kind }

func E(kind error, detail string) error { return &Error{Kind: kind, Detail: detail} }

// HTTPStatus maps a service error to its HTTP status.
func HTTPStatus(err error) int {
	var e *Error
	if errors.As(err, &e) {
		switch e.Kind {
		case ErrNotFound:
			return http.StatusNotFound
		case ErrConflict, ErrPostState, ErrReportState, ErrVersionMismatch,
			ErrNewerVersion, ErrCourseState, ErrTierLimit, ErrAppealUsed,
			ErrPublishContent:
			return http.StatusConflict
		case ErrForbidden, ErrSelfReview, ErrReviewerRenewal:
			return http.StatusForbidden
		case ErrUnauthorized:
			return http.StatusUnauthorized
		case ErrValidation, ErrMaxTiers:
			return http.StatusBadRequest
		}
	}
	switch {
	case errors.Is(err, ErrNotFound):
		return http.StatusNotFound
	case errors.Is(err, ErrForbidden):
		return http.StatusForbidden
	case errors.Is(err, ErrUnauthorized):
		return http.StatusUnauthorized
	}
	return http.StatusInternalServerError
}
