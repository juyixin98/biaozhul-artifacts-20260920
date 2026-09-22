// Package apierror defines sentinel service errors and their HTTP mapping.
package apierror

import (
	"errors"
	"net/http"
)

type Error struct {
	Status  int
	Code    string
	Message string
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

func new(status int, code, msg string) *Error {
	return &Error{Status: status, Code: code, Message: msg}
}

var (
	ErrBadRequest        = new(http.StatusBadRequest, "bad_request", "invalid request")
	ErrInvalidName       = new(http.StatusBadRequest, "invalid_name", "domain name is not valid")
	ErrInvalidYears      = new(http.StatusBadRequest, "invalid_years", "years must be between 1 and 10")
	ErrUnauthorized      = new(http.StatusUnauthorized, "unauthorized", "missing or invalid token")
	ErrForbidden         = new(http.StatusForbidden, "forbidden", "not allowed for this principal")
	ErrBadAuthCode       = new(http.StatusForbidden, "bad_auth_code", "transfer authorization code is incorrect")
	ErrNotFound          = new(http.StatusNotFound, "not_found", "resource not found")
	ErrConflict          = new(http.StatusConflict, "conflict", "resource conflicts with an existing one")
	ErrDomainTaken       = new(http.StatusConflict, "domain_taken", "canonical domain name is already registered")
	ErrInvalidState      = new(http.StatusConflict, "invalid_state", "operation is not allowed in the current state")
	ErrTransferLive      = new(http.StatusConflict, "transfer_live", "a live transfer already exists for this domain")
	ErrSameReseller      = new(http.StatusConflict, "same_reseller", "target reseller must differ from current owner")
	ErrInsufficientFunds = new(http.StatusPaymentRequired, "insufficient_funds", "reseller credit balance is insufficient")
	ErrUnknownTLD        = new(http.StatusUnprocessableEntity, "unknown_tld", "no price rule exists for this TLD")
)

// As extracts an *Error from err.
func As(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}
