package core

import "fmt"

// ErrorCode is a stable machine-readable error code exposed over the API.
type ErrorCode string

const (
	CodeUnknownChain      ErrorCode = "unknown_chain"
	CodeUnknownChannel    ErrorCode = "unknown_channel"
	CodeChannelFrozen     ErrorCode = "channel_frozen"
	CodeBadSignature      ErrorCode = "bad_signature"
	CodeBadProof          ErrorCode = "bad_proof"
	CodeBadRequest        ErrorCode = "bad_request"
	CodeBadParent         ErrorCode = "bad_parent"
	CodeHeaderGap         ErrorCode = "header_gap"
	CodeDuplicateHeader   ErrorCode = "duplicate_header"
	CodeRevokeFirst       ErrorCode = "revoke_tip_first"
	CodeTipNotCurrent     ErrorCode = "tip_not_current"
	CodeBlockFinalized    ErrorCode = "block_finalized"
	CodeBlockUnknown      ErrorCode = "block_unknown"
	CodeBlockNotCanonical ErrorCode = "block_not_canonical"
	CodeEquivocation      ErrorCode = "finalized_equivocation"
	CodeConflict          ErrorCode = "message_conflict"
	CodeConflictFrozen    ErrorCode = "message_conflict_channel_frozen"
	CodeAlreadyExists     ErrorCode = "already_exists"
	CodeInternal          ErrorCode = "internal_error"
)

// DomainError carries a stable code and a human-readable message.
type DomainError struct {
	Code ErrorCode
	Msg  string
}

func (e *DomainError) Error() string { return string(e.Code) + ": " + e.Msg }

func domErr(code ErrorCode, format string, args ...any) error {
	return &DomainError{Code: code, Msg: fmt.Sprintf(format, args...)}
}
