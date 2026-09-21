package domain

import (
	"errors"
	"time"
)

// State machine for a payment:
//
//	authorized ──capture──> captured ──settle──> settled
//	    │                     │  │                 │
//	    │ (partial capture    │  └──refund──> partially_refunded ──> refunded
//	    │  stays captured)    └──full refund──> refunded
//	    └──void(<=24h)──> voided   (terminal)
//
// Refunds are allowed while captured/partially_refunded (before settlement,
// any amount) and within 90 days after settlement. After the 90-day window
// refunds are rejected. Captures and voids must happen within 24h of the
// authorization.

const (
	StatusAuthorized        = "authorized"
	StatusCaptured          = "captured"
	StatusPartiallyRefunded = "partially_refunded"
	StatusRefunded          = "refunded"
	StatusVoided            = "voided"
	StatusSettled           = "settled"
)

const (
	AuthWindow   = 24 * time.Hour
	RefundWindow = 90 * 24 * time.Hour
)

// Sentinel domain errors. HTTP mapping lives in the api package.
var (
	ErrNotFound            = errors.New("resource not found")
	ErrForbidden           = errors.New("forbidden")
	ErrConflict            = errors.New("conflict")
	ErrInvalidState        = errors.New("illegal state transition")
	ErrAuthExpired         = errors.New("authorization has expired (24h window)")
	ErrRefundWindowClosed  = errors.New("refund window has closed (90 days after settlement)")
	ErrRefundTooLarge      = errors.New("cumulative refunds cannot exceed captured amount")
	ErrAmountInvalid       = errors.New("amount must be positive")
	ErrCaptureTooLarge     = errors.New("capture cannot exceed authorized amount")
	ErrIdempotencyConflict = errors.New("idempotency key was already used with different parameters")
	ErrSettlementExists    = errors.New("settlement for this merchant and date already exists")
	ErrReconInProgress     = errors.New("reconciliation already running for this merchant and date")
	ErrValidation          = errors.New("validation error")
)
