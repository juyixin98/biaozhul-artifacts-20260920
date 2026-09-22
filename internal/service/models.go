package service

import "time"

// Domain lifecycle statuses. A name with no row in domains is "available".
const (
	StatusRegistered    = "registered"
	StatusTransferring  = "transferring"
	StatusExpired       = "expired"
	StatusRedemption    = "redemption"
	StatusPendingDelete = "pending_delete"
)

// Legal transitions (all guarded by conditional UPDATEs or row locks):
//
//	available      -> registered      Register (billing: charge register price)
//	registered     -> registered      Renew    (billing: charge renew price, expiry extended)
//	expired        -> registered      Renew    (billing: charge renew price)
//	redemption     -> registered      Renew    (billing: charge redeem price)
//	registered     -> transferring    InitiateTransfer (billing: freeze transfer price)
//	transferring   -> registered      Cancel/Reject/Timeout (billing: release freeze)
//	transferring   -> registered      CompleteTransfer (billing: capture freeze, +1 year)
//	registered     -> expired         maintenance: expires_at <= now
//	expired        -> redemption      maintenance: expired grace elapsed
//	redemption     -> pending_delete  maintenance: 30-day redemption elapsed
//	pending_delete -> (row deleted)   maintenance: pending-delete elapsed -> available
const (
	TransferPending   = "pending_approval"
	TransferApproved  = "approved"
	TransferCompleted = "completed"
	TransferCancelled = "cancelled"
	TransferFailed    = "failed"
)

const (
	ActionRegister = "register"
	ActionRenew    = "renew"
	ActionRedeem   = "redeem"
	ActionTransfer = "transfer"
)

const (
	RoleAdmin    = "admin"
	RoleReseller = "reseller"
	RoleCustomer = "customer"
)

type User struct {
	ID         string  `db:"id" json:"id"`
	APIKey     string  `db:"api_key" json:"-"`
	Role       string  `db:"role" json:"role"`
	Name       string  `db:"name" json:"name"`
	ResellerID *string `db:"reseller_id" json:"reseller_id,omitempty"`
}

type Domain struct {
	ID        string    `db:"id" json:"id"`
	Name      string    `db:"name" json:"name"`
	OwnerID   string    `db:"owner_id" json:"owner_id"`
	Status    string    `db:"status" json:"status"`
	ExpiresAt time.Time `db:"expires_at" json:"expires_at"`
	Version   int64     `db:"version" json:"version"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
	UpdatedAt time.Time `db:"updated_at" json:"updated_at"`
}

type Transfer struct {
	ID               string     `db:"id" json:"id"`
	DomainID         string     `db:"domain_id" json:"domain_id"`
	DomainName       string     `db:"domain_name" json:"domain_name"`
	FromOwnerID      string     `db:"from_owner_id" json:"from_owner_id"`
	ToOwnerID        string     `db:"to_owner_id" json:"to_owner_id"`
	State            string     `db:"state" json:"state"`
	PriceCents       int64      `db:"price_cents" json:"price_cents"`
	HoldID           string     `db:"hold_id" json:"-"`
	ApprovalDeadline time.Time  `db:"approval_deadline" json:"approval_deadline"`
	CompletesAt      *time.Time `db:"completes_at" json:"completes_at,omitempty"`
	CancelReason     *string    `db:"cancel_reason" json:"cancel_reason,omitempty"`
	CreatedAt        time.Time  `db:"created_at" json:"created_at"`
}

type LedgerEntry struct {
	ID             int64     `db:"id" json:"id"`
	ResellerID     string    `db:"reseller_id" json:"reseller_id"`
	IdempotencyKey string    `db:"idempotency_key" json:"idempotency_key"`
	Kind           string    `db:"kind" json:"kind"` // credit | charge
	AmountCents    int64     `db:"amount_cents" json:"amount_cents"`
	Memo           string    `db:"memo" json:"memo"`
	DomainName     *string   `db:"domain_name" json:"domain_name,omitempty"`
	TransferID     *string   `db:"transfer_id" json:"transfer_id,omitempty"`
	CreatedAt      time.Time `db:"created_at" json:"created_at"`
}

type Balance struct {
	ResellerID     string `json:"reseller_id"`
	BalanceCents   int64  `json:"balance_cents"`
	HeldCents      int64  `json:"held_cents"`
	AvailableCents int64  `json:"available_cents"`
}
