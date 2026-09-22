// Package models holds the persisted domain types shared across services.
package models

import "time"

// Domain lifecycle statuses. "Available" is not a row state: it is the absence
// of a domains row for a given canonical name.
const (
	StatusRegistered    = "registered"
	StatusExpired       = "expired"
	StatusRedeemable    = "redeemable"
	StatusPendingDelete = "pending_delete"
	StatusTransferring  = "transferring"
)

// Transfer states.
const (
	TransferPending  = "pending_approval" // seller must approve (or auto-approve window)
	TransferApproved = "seller_approved"  // seller approved; buyer-5-day clock running
	TransferDone     = "completed"
	TransferRejected = "rejected"
	TransferCanceled = "canceled"
	TransferFailed   = "failed" // approval-window TTL elapsed without buyer approval
)

// Ledger transaction kinds.
const (
	TxTopup           = "topup"
	TxRegister        = "register"
	TxRenew           = "renew"
	TxRestore         = "restore"
	TxTransferFreeze  = "transfer_freeze"
	TxTransferCapture = "transfer_capture"
	TxTransferRelease = "transfer_release"
)

// Domain event types.
const (
	EventRegistered        = "registered"
	EventRenewed           = "renewed"
	EventExpired           = "expired"
	EventEnteredRedeem     = "entered_redeem"
	EventEnteredPending    = "entered_pending_delete"
	EventPurged            = "purged"
	EventRestored          = "restored"
	EventAuthRotated       = "authcode_rotated"
	EventTransferRequested = "transfer_requested"
	EventTransferApproved  = "transfer_seller_approved"
	EventTransferRejected  = "transfer_rejected"
	EventTransferCanceled  = "transfer_canceled"
	EventTransferFailed    = "transfer_failed"
	EventTransferCompleted = "transfer_completed"
)

type Reseller struct {
	ID           int64     `db:"id" json:"id"`
	Name         string    `db:"name" json:"name"`
	BalanceCents int64     `db:"balance_cents" json:"balance_cents"`
	HeldCents    int64     `db:"held_cents" json:"held_cents"`
	CreatedAt    time.Time `db:"created_at" json:"created_at"`
}

type Customer struct {
	ID         int64     `db:"id" json:"id"`
	ResellerID int64     `db:"reseller_id" json:"reseller_id"`
	Name       string    `db:"name" json:"name"`
	CreatedAt  time.Time `db:"created_at" json:"created_at"`
}

type Principal struct {
	ID         int64     `db:"id"`
	Role       string    `db:"role"`
	ResellerID *int64    `db:"reseller_id"`
	CustomerID *int64    `db:"customer_id"`
	Username   string    `db:"username"`
	TokenHash  string    `db:"token_hash"`
	CreatedAt  time.Time `db:"created_at"`
}

type PriceRule struct {
	ID            int64     `db:"id" json:"id"`
	TLD           string    `db:"tld" json:"tld"`
	RegisterCents int64     `db:"register_cents" json:"register_cents"`
	RenewCents    int64     `db:"renew_cents" json:"renew_cents"`
	RestoreCents  int64     `db:"restore_cents" json:"restore_cents"`
	TransferCents int64     `db:"transfer_cents" json:"transfer_cents"`
	Superseded    bool      `db:"superseded" json:"superseded"`
	CreatedAt     time.Time `db:"created_at" json:"created_at"`
}

type Domain struct {
	ID              int64      `db:"id" json:"id"`
	Name            string     `db:"name" json:"name"`
	TLD             string     `db:"tld" json:"tld"`
	CanonicalName   string     `db:"canonical_name" json:"canonical_name"`
	CustomerID      int64      `db:"customer_id" json:"customer_id"`
	ResellerID      int64      `db:"reseller_id" json:"reseller_id"`
	Status          string     `db:"status" json:"status"`
	ExpiresAt       time.Time  `db:"expires_at" json:"expires_at"`
	ExpiredAt       *time.Time `db:"expired_at" json:"expired_at,omitempty"`
	RedeemableAt    *time.Time `db:"redeemable_at" json:"redeemable_at,omitempty"`
	PendingDeleteAt *time.Time `db:"pending_delete_at" json:"pending_delete_at,omitempty"`
	PurgeAt         *time.Time `db:"purge_at" json:"purge_at,omitempty"`
	AuthCipher      string     `db:"auth_cipher" json:"-"`
	CreatedAt       time.Time  `db:"created_at" json:"created_at"`
	UpdatedAt       time.Time  `db:"updated_at" json:"updated_at"`
}

type Transfer struct {
	ID               int64      `db:"id" json:"id"`
	DomainID         int64      `db:"domain_id" json:"domain_id"`
	DomainName       string     `db:"domain_name" json:"domain_name"`
	FromResellerID   int64      `db:"from_reseller_id" json:"from_reseller_id"`
	FromCustomerID   int64      `db:"from_customer_id" json:"from_customer_id"`
	ToResellerID     int64      `db:"to_reseller_id" json:"to_reseller_id"`
	ToCustomerID     int64      `db:"to_customer_id" json:"to_customer_id"`
	State            string     `db:"state" json:"state"`
	TransferCents    int64      `db:"transfer_cents" json:"transfer_cents"`
	RequestedAt      time.Time  `db:"requested_at" json:"requested_at"`
	ApprovedAt       *time.Time `db:"approved_at" json:"approved_at,omitempty"`
	CompletedAt      *time.Time `db:"completed_at" json:"completed_at,omitempty"`
	ApprovalDeadline time.Time  `db:"approval_deadline" json:"approval_deadline"`
	ApproveUntil     *time.Time `db:"approve_until" json:"approve_until,omitempty"`
	ExpiresAt        time.Time  `db:"expires_at" json:"expires_at"`
	CreatedTxID      *int64     `db:"created_tx_id" json:"created_tx_id,omitempty"`
	CapturedTxID     *int64     `db:"captured_tx_id" json:"captured_tx_id,omitempty"`
	CreatedAt        time.Time  `db:"created_at" json:"created_at"`
	UpdatedAt        time.Time  `db:"updated_at" json:"updated_at"`
}

type BillingTransaction struct {
	ID             int64     `db:"id" json:"id"`
	ResellerID     int64     `db:"reseller_id" json:"reseller_id"`
	Kind           string    `db:"kind" json:"kind"`
	AmountCents    int64     `db:"amount_cents" json:"amount_cents"`
	Status         string    `db:"status" json:"status"`
	DomainID       *int64    `db:"domain_id" json:"domain_id,omitempty"`
	TransferID     *int64    `db:"transfer_id" json:"transfer_id,omitempty"`
	Years          *int      `db:"years" json:"years,omitempty"`
	IdempotencyKey *string   `db:"idempotency_key" json:"idempotency_key,omitempty"`
	CreatedAt      time.Time `db:"created_at" json:"created_at"`
}

type DomainEvent struct {
	ID          int64     `db:"id" json:"id"`
	DomainID    int64     `db:"domain_id" json:"domain_id"`
	DomainName  string    `db:"domain_name" json:"domain_name"`
	EventType   string    `db:"event_type" json:"event_type"`
	FromStatus  *string   `db:"from_status" json:"from_status,omitempty"`
	ToStatus    *string   `db:"to_status" json:"to_status,omitempty"`
	AmountCents *int64    `db:"amount_cents" json:"amount_cents,omitempty"`
	Detail      []byte    `db:"detail" json:"detail,omitempty"`
	CreatedAt   time.Time `db:"created_at" json:"created_at"`
}
