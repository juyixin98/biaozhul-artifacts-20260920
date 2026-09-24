package attestation

import (
	"regexp"
	"time"
)

var (
	sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)
	commitRev = regexp.MustCompile(`^(?:sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})$`)
	nonceRev  = regexp.MustCompile(`^[A-Za-z0-9_-]{16,64}$`)
	builderRe = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{2,63}$`)
)

func validateStatementShape(st *Statement) *VerificationError {
	if st.Type != StatementType {
		return verr(CodeCrossPurpose, "statement _type must be %q, got %q", StatementType, st.Type)
	}
	if !builderRe.MatchString(st.BuilderID) {
		return verr(CodeStatementInvalid, "builderId must match %s", builderRe.String())
	}
	if st.KeyID == "" || len(st.KeyID) > 128 {
		return verr(CodeStatementInvalid, "keyId is required (max 128 chars)")
	}
	if st.Source.Repository == "" {
		return verr(CodeStatementInvalid, "source.repository is required")
	}
	if !commitRev.MatchString(st.Source.Commit) {
		return verr(CodeStatementInvalid, "source.commit must be sha1:<40 hex> or sha256:<64 hex>")
	}
	if len(st.Subjects) == 0 {
		return verr(CodeStatementInvalid, "at least one subject digest is required")
	}
	for _, d := range st.Subjects {
		if d.Alg != "sha256" || !sha256Hex.MatchString(d.Value) {
			return verr(CodeStatementInvalid, "subjects must use alg=sha256 with 64 lowercase hex chars")
		}
	}
	if st.Params == nil {
		return verr(CodeStatementInvalid, "params object is required (may be empty)")
	}
	if !nonceRev.MatchString(st.Nonce) {
		return verr(CodeStatementInvalid, "nonce must be 16-64 chars of [A-Za-z0-9_-]")
	}
	issued, err := time.Parse(time.RFC3339, st.IssuedAt)
	if err != nil {
		return verr(CodeStatementInvalid, "issuedAt must be RFC3339 UTC timestamp: %v", err)
	}
	if issued.Location() != time.UTC {
		return verr(CodeStatementInvalid, "issuedAt must be expressed in UTC (Z suffix)")
	}
	return nil
}
