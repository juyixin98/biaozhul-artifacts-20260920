package service

import (
	"fmt"
	"net/http"
)

// APIError is an error that maps directly to an HTTP response.
type APIError struct {
	Status  int
	Code    string
	Message string
}

func (e *APIError) Error() string { return e.Message }

func apiError(status int, code, msg string) *APIError {
	return &APIError{Status: status, Code: code, Message: msg}
}

func ErrValidation(format string, args ...any) *APIError {
	return apiError(http.StatusBadRequest, "validation", fmt.Sprintf(format, args...))
}

func ErrInvalidState(format string, args ...any) *APIError {
	return apiError(http.StatusConflict, "invalid_state", fmt.Sprintf(format, args...))
}

var (
	ErrNotFound            = apiError(http.StatusNotFound, "not_found", "resource not found")
	ErrForbidden           = apiError(http.StatusForbidden, "forbidden", "you do not have access to this resource")
	ErrUnauthorized        = apiError(http.StatusUnauthorized, "unauthorized", "missing or invalid API key")
	ErrDomainTaken         = apiError(http.StatusConflict, "domain_taken", "domain is already registered")
	ErrInsufficientCredits = apiError(http.StatusPaymentRequired, "insufficient_credits", "reseller has insufficient available credits")
	ErrInvalidAuthCode     = apiError(http.StatusForbidden, "invalid_auth_code", "transfer authorization code is invalid")
	ErrNoPrice             = apiError(http.StatusUnprocessableEntity, "no_price", "no price configured for this TLD and action")
)
