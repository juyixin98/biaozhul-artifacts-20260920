// Package service contains the business logic of the revocable credential
// index: real Ed25519 issuance, append-only revocation and historical
// replay verification keyed by snapshot.
package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"vci/internal/clock"
	"vci/internal/crypto"
	"vci/internal/domain"
)

// Service wires the store, clock and the verification cache.
type Service struct {
	store domain.Store
	clk   clock.Clock
	cache *snapshotCache
}

func New(st domain.Store, clk clock.Clock) *Service {
	if clk == nil {
		clk = clock.System{}
	}
	return &Service{store: st, clk: clk, cache: newSnapshotCache(defaultCacheTTL, clk.Now)}
}

// ---------------- requests / responses ----------------

type IssuerView struct {
	ID        string `json:"issuer_id"`
	Name      string `json:"name"`
	CreatedAt string `json:"created_at"`
	Snapshot  int64  `json:"snapshot"`
}

type KeyView struct {
	ID        string  `json:"kid"`
	IssuerID  string  `json:"issuer_id"`
	Seq       int     `json:"seq"`
	Algorithm string  `json:"algorithm"`
	ValidFrom string  `json:"valid_from"`
	RetiredAt *string `json:"retired_at,omitempty"`
	Snapshot  int64   `json:"snapshot"`
}

// Keypair is returned at creation/rotation time ONLY, for the synthetic
// test deployment. It is needed so the operator can inspect signatures,
// but real systems must never hand private keys over an HTTP API.
type Keypair struct {
	Kid        string `json:"kid"`
	PublicKey  string `json:"public_key"`
	PrivateKey string `json:"private_key"`
	Algorithm  string `json:"algorithm"`
	Warning    string `json:"warning"`
}

type CreateIssuerResult struct {
	Issuer  IssuerView `json:"issuer"`
	Keypair Keypair    `json:"genesis_key"`
}

type RotateKeyResult struct {
	Key     KeyView `json:"key"`
	Keypair Keypair `json:"keypair"`
	OldKey  KeyView `json:"previous_key"`
}

type IssueRequest struct {
	IssuerID  string          `json:"issuer_id"`
	Subject   string          `json:"subject"`
	Purpose   string          `json:"purpose"`
	NotBefore *time.Time      `json:"not_before,omitempty"`
	ExpiresAt time.Time       `json:"expires_at"`
	Content   json.RawMessage `json:"content"`
}

type CredentialView struct {
	ID          string          `json:"credential_id"`
	IssuerID    string          `json:"issuer_id"`
	Subject     string          `json:"subject"`
	Purpose     string          `json:"purpose"`
	NotBefore   string          `json:"not_before"`
	ExpiresAt   string          `json:"expires_at"`
	IssuedAt    string          `json:"issued_at"`
	ContentHash string          `json:"content_hash"`
	Kid         string          `json:"kid"`
	Snapshot    int64           `json:"snapshot"`
	Payload     string          `json:"payload_b64"`
	Content     json.RawMessage `json:"content"`
	Signature   string          `json:"signature_b64"`
}

type RevokeRequest struct {
	CredentialID string     `json:"credential_id"`
	Reason       string     `json:"reason"`
	EffectiveAt  *time.Time `json:"effective_at,omitempty"` // default now; may be backdated
}

type RevocationView struct {
	ID           string `json:"revocation_id"`
	CredentialID string `json:"credential_id"`
	Reason       string `json:"reason"`
	EffectiveAt  string `json:"effective_at"`
	CreatedAt    string `json:"created_at"`
	Snapshot     int64  `json:"snapshot"`
}

type VerifyRequest struct {
	CredentialID    string `json:"credential_id"`
	ExpectedPurpose string `json:"expected_purpose,omitempty"`
	// AsOf is the historical query time (RFC3339). Empty means now. The
	// verdict is computed by REPLAYING history up to this instant — never
	// from current state.
	AsOf *time.Time `json:"as_of,omitempty"`
}

type VerifyResult struct {
	CredentialID string                     `json:"credential_id"`
	Verdict      domain.VerificationVerdict `json:"verdict"`
	Reason       string                     `json:"reason,omitempty"`
	Snapshot     int64                      `json:"snapshot"`
	AsOf         string                     `json:"as_of"`
	CacheHit     bool                       `json:"cache_hit"`
	Detail       *VerifyDetail              `json:"detail,omitempty"`
}

type VerifyDetail struct {
	Subject      string  `json:"subject"`
	Purpose      string  `json:"purpose"`
	ContentHash  string  `json:"content_hash"`
	Kid          string  `json:"kid"`
	IssuedAt     string  `json:"issued_at"`
	NotBefore    string  `json:"not_before"`
	ExpiresAt    string  `json:"expires_at"`
	RevocationID *string `json:"revocation_id,omitempty"`
	RevokedAt    *string `json:"revoked_effective_at,omitempty"`
}

// ---------------- operations ----------------

func (s *Service) CreateIssuer(ctx context.Context, name string) (CreateIssuerResult, error) {
	if name == "" {
		return CreateIssuerResult{}, fmt.Errorf("%w: name required", domain.ErrConflict)
	}
	pub, priv, err := crypto.GenerateEd25519Key()
	if err != nil {
		return CreateIssuerResult{}, err
	}
	now := s.clk.Now()
	iss, key, err := s.store.CreateIssuer(ctx, name, crypto.EncodePublic(pub), crypto.EncodePrivate(priv), now)
	if err != nil {
		return CreateIssuerResult{}, err
	}
	return CreateIssuerResult{
		Issuer: IssuerView{
			ID: iss.ID, Name: iss.Name, CreatedAt: rfc(iss.CreatedAt), Snapshot: iss.Snapshot,
		},
		Keypair: keypairOf(key),
	}, nil
}

func (s *Service) RotateKey(ctx context.Context, issuerID string, closeOld bool) (RotateKeyResult, error) {
	if issuerID == "" {
		return RotateKeyResult{}, fmt.Errorf("%w: issuer_id required", domain.ErrConflict)
	}
	pub, priv, err := crypto.GenerateEd25519Key()
	if err != nil {
		return RotateKeyResult{}, err
	}
	now := s.clk.Now()
	nk, old, err := s.store.RotateKey(ctx, issuerID, crypto.EncodePublic(pub), crypto.EncodePrivate(priv), now, closeOld)
	if err != nil {
		return RotateKeyResult{}, err
	}
	oldView := KeyView{
		ID: old.ID, IssuerID: old.IssuerID, Seq: old.Seq, Algorithm: old.Algorithm,
		ValidFrom: rfc(old.ValidFrom), RetiredAt: rfcPtr(old.RetiredAt), Snapshot: old.Snapshot,
	}
	return RotateKeyResult{
		Key: KeyView{
			ID: nk.ID, IssuerID: nk.IssuerID, Seq: nk.Seq, Algorithm: nk.Algorithm,
			ValidFrom: rfc(nk.ValidFrom), RetiredAt: rfcPtr(nk.RetiredAt), Snapshot: nk.Snapshot,
		},
		Keypair: keypairOf(nk),
		OldKey:  oldView,
	}, nil
}

func (s *Service) RetireKey(ctx context.Context, issuerID string) (KeyView, error) {
	if issuerID == "" {
		return KeyView{}, fmt.Errorf("%w: issuer_id required", domain.ErrConflict)
	}
	k, err := s.store.RetireActiveKey(ctx, issuerID, s.clk.Now())
	if err != nil {
		return KeyView{}, err
	}
	return KeyView{
		ID: k.ID, IssuerID: k.IssuerID, Seq: k.Seq, Algorithm: k.Algorithm,
		ValidFrom: rfc(k.ValidFrom), RetiredAt: rfcPtr(k.RetiredAt), Snapshot: k.Snapshot,
	}, nil
}

// signedPayload is the exact JSON document covered by the signature.
type signedPayload struct {
	Version     int       `json:"v"`
	Kid         string    `json:"kid"`
	IssuerID    string    `json:"issuer_id"`
	Subject     string    `json:"subject"`
	Purpose     string    `json:"purpose"`
	NotBefore   time.Time `json:"not_before"`
	ExpiresAt   time.Time `json:"expires_at"`
	IssuedAt    time.Time `json:"issued_at"`
	ContentHash string    `json:"content_hash"`
}

func (s *Service) IssueCredential(ctx context.Context, req IssueRequest) (CredentialView, error) {
	if req.IssuerID == "" || req.Subject == "" || req.Purpose == "" {
		return CredentialView{}, fmt.Errorf("%w: issuer_id, subject and purpose are required", domain.ErrConflict)
	}
	if len(req.Content) == 0 {
		return CredentialView{}, fmt.Errorf("%w: content required", domain.ErrConflict)
	}
	// Decode content into generic form so we can re-encode it canonically.
	var content interface{}
	dec := json.NewDecoder(bytes.NewReader(req.Content))
	dec.UseNumber()
	if err := dec.Decode(&content); err != nil {
		return CredentialView{}, fmt.Errorf("%w: content is not valid JSON: %v", domain.ErrConflict, err)
	}

	notBefore := s.clk.Now()
	if req.NotBefore != nil {
		notBefore = req.NotBefore.UTC()
	}
	// Sample "now" exactly once for the whole issuance so issued_at, the
	// signing payload and the store row cannot straddle a clock tick.
	now := s.clk.Now()
	expiresAt := req.ExpiresAt.UTC()
	if !expiresAt.After(notBefore) {
		return CredentialView{}, fmt.Errorf("%w: expires_at must be after not_before", domain.ErrConflict)
	}

	contentJSON, err := crypto.CanonicalJSON(content)
	if err != nil {
		return CredentialView{}, err
	}
	contentHash, err := crypto.ContentDigest(content)
	if err != nil {
		return CredentialView{}, err
	}

	sign := func(k domain.KeyVersion, issuedAt time.Time) ([]byte, []byte, []byte, string, error) {
		priv, err := crypto.DecodePrivate(k.PrivateKey)
		if err != nil {
			return nil, nil, nil, "", err
		}
		pl := signedPayload{
			Version:     1,
			Kid:         k.ID,
			IssuerID:    k.IssuerID,
			Subject:     req.Subject,
			Purpose:     req.Purpose,
			NotBefore:   notBefore,
			ExpiresAt:   expiresAt,
			IssuedAt:    issuedAt,
			ContentHash: contentHash,
		}
		payloadJSON, err := canonicalStruct(pl)
		if err != nil {
			return nil, nil, nil, "", err
		}
		sig := crypto.Sign(priv, payloadJSON) // REAL Ed25519 signing
		return payloadJSON, contentJSON, sig, contentHash, nil
	}

	c, _, err := s.store.IssueCredential(ctx, req.IssuerID, req.Subject, req.Purpose,
		notBefore, expiresAt, now, sign)
	if err != nil {
		return CredentialView{}, err
	}
	return credentialView(c, contentJSON), nil
}

func (s *Service) Revoke(ctx context.Context, req RevokeRequest) (RevocationView, error) {
	if req.CredentialID == "" {
		return RevocationView{}, fmt.Errorf("%w: credential_id required", domain.ErrConflict)
	}
	now := s.clk.Now()
	effective := now
	if req.EffectiveAt != nil {
		// effective_at may be backdated (late-recorded event) or in the
		// future (a revocation scheduled to take effect later); replay at
		// as_of decides whether it applies. Recording the event itself
		// advances the snapshot immediately so caches are invalidated now.
		effective = req.EffectiveAt.UTC()
	}
	reason := req.Reason
	if reason == "" {
		reason = "unspecified"
	}
	rev, err := s.store.Revoke(ctx, req.CredentialID, reason, effective, now)
	if err != nil {
		return RevocationView{}, err
	}
	return RevocationView{
		ID: rev.ID, CredentialID: rev.CredentialID, Reason: rev.Reason,
		EffectiveAt: rfc(rev.EffectiveAt), CreatedAt: rfc(rev.CreatedAt), Snapshot: rev.Snapshot,
	}, nil
}

// VerifyCredential performs the historical replay. Snapshot semantics:
//
//  1. read locked state; store returns state + head snapshot S;
//  2. compute verdict entirely from that frozen state at asOf;
//  3. cache key includes S, so ANY append (including a revoke that
//     commits while we computed) makes the old entry unreachable.
func (s *Service) VerifyCredential(ctx context.Context, req VerifyRequest) (VerifyResult, error) {
	return s.verifyCredential(ctx, req, 0)
}

func (s *Service) verifyCredential(ctx context.Context, req VerifyRequest, attempt int) (VerifyResult, error) {
	if req.CredentialID == "" {
		return VerifyResult{}, fmt.Errorf("%w: credential_id required", domain.ErrConflict)
	}
	// asOfKey distinguishes a historical query (absolute instant) from a
	// "current" query. Current queries share one cache slot per
	// (credential, purpose, snapshot) even though wall time moves between
	// requests — the verdict is computed against the same frozen snapshot
	// state, and revocation moves the snapshot, which misses the slot.
	now := s.clk.Now()
	asOf := now
	asOfKey := time.Time{} // zero sentinel = "current"
	if req.AsOf != nil {
		asOf = req.AsOf.UTC()
		asOfKey = asOf
	}

	// Cache is keyed by (query, head-at-read); we still need the store read
	// to know the head. To avoid paying that cost on a hot key, peek at the
	// global head first, then check the cache; the locked read in
	// replay() re-validates the head.
	head, err := s.store.Head(ctx)
	if err != nil {
		return VerifyResult{}, err
	}
	ck := cacheKey{
		credential: req.CredentialID,
		purpose:    req.ExpectedPurpose,
		asOf:       asOfKey,
		snapshot:   head,
	}
	if v, ok := s.cache.get(ck, now); ok {
		v.CacheHit = true
		return v, nil
	}

	st, err := s.store.ReadVerificationState(ctx, req.CredentialID)
	if err != nil {
		return VerifyResult{}, err
	}
	// Re-read the head after releasing the read locks: if another
	// transaction committed an append while we were computing, the head
	// moved past the snapshot embedded in `st`. Recomputing against the
	// newer state keeps revoke/verify snapshot equality exact even under
	// scheduling noise. Bounded to avoid spinning under a constant write
	// stream (the cache key alone already guarantees correctness; this is
	// about the number returned on the very first read after a write).
	finalHead, err := s.store.Head(ctx)
	if err != nil {
		return VerifyResult{}, err
	}
	if finalHead > st.Snapshot && attempt < 3 {
		return s.verifyCredential(ctx, req, attempt+1)
	}
	res := s.replay(st, req.ExpectedPurpose, asOf)
	ck.snapshot = st.Snapshot
	if req.AsOf == nil {
		s.cache.put(ck, res, freshBoundary(st, now))
	} else {
		s.cache.put(ck, res, time.Time{})
	}
	return res, nil
}

// replay contains the pure decision logic over frozen state.
func (s *Service) replay(st domain.VerificationState, expectedPurpose string, asOf time.Time) VerifyResult {
	res := VerifyResult{
		CredentialID: "",
		Verdict:      domain.VerdictUnknown,
		Snapshot:     st.Snapshot,
		AsOf:         rfc(asOf),
	}
	c := st.Credential
	if c == nil {
		res.Verdict = domain.VerdictUnknown
		res.Reason = "credential does not exist"
		return res
	}
	res.CredentialID = c.ID
	detail := &VerifyDetail{
		Subject:     c.Subject,
		Purpose:     c.Purpose,
		ContentHash: c.ContentHash,
		Kid:         c.KeyID,
		IssuedAt:    rfc(c.IssuedAt),
		NotBefore:   rfc(c.NotBefore),
		ExpiresAt:   rfc(c.ExpiresAt),
	}
	res.Detail = detail

	// 0. Historical replay: the credential had not yet been issued. A
	// credential that does not exist at as_of is UNKNOWN regardless of
	// later events (a revocation recorded afterwards cannot revoke a
	// credential before it existed, and the validity window is irrelevant
	// to an observer at that earlier instant).
	if asOf.Before(c.IssuedAt) {
		res.Verdict = domain.VerdictUnknown
		res.Reason = "credential not yet issued at as_of"
		return res
	}

	// 1. Revocation replay: earliest event effective at or before asOf.
	if st.Revocation != nil && !asOf.Before(st.Revocation.EffectiveAt) {
		res.Verdict = domain.VerdictRevoked
		res.Reason = "revoked: " + st.Revocation.Reason
		rid := st.Revocation.ID
		ea := rfc(st.Revocation.EffectiveAt)
		detail.RevocationID = &rid
		detail.RevokedAt = &ea
		return res
	}

	// 2. Validity interval at asOf (inclusive lower, exclusive upper — the
	// exact expiry-instant boundary means EXPIRED).
	if asOf.Before(c.NotBefore) {
		res.Verdict = domain.VerdictExpired
		res.Reason = "not yet valid at as_of"
		return res
	}
	if !asOf.Before(c.ExpiresAt) {
		res.Verdict = domain.VerdictExpired
		res.Reason = "expired at as_of (boundary is exclusive)"
		return res
	}

	// 3. Key lifecycle at ISSUANCE: the credential must have been signed by
	// a key version already valid at that instant. Rotation preserves the
	// old interval (retired_at = the next key's valid_from), so a key
	// retired LATER never invalidates a historical signature — the replay
	// of an old credential under a since-rotated key remains VALID. A
	// credential whose key's valid_from is after issued_at can only arise
	// through tampering/misuse and is rejected.
	if st.Key == nil {
		res.Verdict = domain.VerdictInvalidSignature
		res.Reason = "signing key version missing"
		return res
	}
	if c.IssuedAt.Before(st.Key.ValidFrom) {
		res.Verdict = domain.VerdictKeyInactive
		res.Reason = "signing key was not valid when the credential was issued"
		return res
	}

	// 4. REAL Ed25519 verification over the exact stored payload bytes.
	pub, err := crypto.DecodePublic(st.Key.PublicKey)
	if err != nil {
		res.Verdict = domain.VerdictInvalidSignature
		res.Reason = "malformed public key: " + err.Error()
		return res
	}
	if err := crypto.Verify(pub, c.PayloadJSON, c.Signature); err != nil {
		res.Verdict = domain.VerdictInvalidSignature
		res.Reason = err.Error()
		return res
	}

	// 5. Tampering: recompute the content digest from the stored content.
	ok, err := crypto.VerifyDigest(c.ContentHash, mustGeneric(c.ContentJSON))
	if err != nil || !ok {
		res.Verdict = domain.VerdictContentMismatch
		res.Reason = "stored content does not match content_hash"
		return res
	}

	// 6. Purpose binding.
	if expectedPurpose != "" && expectedPurpose != c.Purpose {
		res.Verdict = domain.VerdictPurposeMismatch
		res.Reason = fmt.Sprintf("purpose %q does not match expected %q", c.Purpose, expectedPurpose)
		return res
	}

	res.Verdict = domain.VerdictValid
	return res
}

// VerifyCredentialByContent additionally compares a caller-supplied
// content body against the recorded digest (used by the verify endpoint).
func (s *Service) VerifyCredentialByContent(ctx context.Context, req VerifyRequest, suppliedContent json.RawMessage) (VerifyResult, error) {
	res, err := s.VerifyCredential(ctx, req)
	if err != nil {
		return res, err
	}
	if len(suppliedContent) > 0 && res.Verdict == domain.VerdictValid {
		var supplied interface{}
		dec := json.NewDecoder(bytes.NewReader(suppliedContent))
		dec.UseNumber()
		if err := dec.Decode(&supplied); err != nil {
			return res, fmt.Errorf("%w: supplied content is not valid JSON", domain.ErrConflict)
		}
		want, err := crypto.ContentDigest(supplied)
		if err != nil {
			return res, err
		}
		if want != res.Detail.ContentHash {
			res.Verdict = domain.VerdictContentMismatch
			res.Reason = "supplied content digest differs from credential content_hash"
		}
	}
	return res, nil
}

// Head exposes the global snapshot (useful for tests/monitoring).
func (s *Service) Head(ctx context.Context) (int64, error) { return s.store.Head(ctx) }

// CacheStats returns approximate cache size (tests).
func (s *Service) CacheStats() int { return s.cache.size() }

// ---------------- small helpers ----------------

func rfc(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
func rfcPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := rfc(*t)
	return &s
}

func keypairOf(k domain.KeyVersion) Keypair {
	return Keypair{
		Kid: k.ID, PublicKey: k.PublicKey, PrivateKey: k.PrivateKey,
		Algorithm: k.Algorithm,
		Warning:   "SYNTHETIC TEST KEY MATERIAL — never use in production",
	}
}

func credentialView(c domain.Credential, contentJSON []byte) CredentialView {
	return CredentialView{
		ID: c.ID, IssuerID: c.IssuerID, Subject: c.Subject, Purpose: c.Purpose,
		NotBefore: rfc(c.NotBefore), ExpiresAt: rfc(c.ExpiresAt), IssuedAt: rfc(c.IssuedAt),
		ContentHash: c.ContentHash, Kid: c.KeyID, Snapshot: c.Snapshot,
		Payload:   crypto.B64(c.PayloadJSON),
		Content:   json.RawMessage(contentJSON),
		Signature: crypto.B64(c.Signature),
	}
}

func mustGeneric(b []byte) interface{} {
	var v interface{}
	_ = json.Unmarshal(b, &v)
	return v
}

// IsDomainError maps sentinel errors for handlers/tests.
func IsDomainError(err error, target error) bool { return errors.Is(err, target) }
