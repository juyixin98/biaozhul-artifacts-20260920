// Package memstore is an in-memory domain.Store used by unit tests so the
// replay/cache/signature logic can be exercised without PostgreSQL. It
// models the same append-only and one-revocation semantics as the Postgres
// store; concurrency on individual appends is serialized by one mutex.
package memstore

import (
	"context"
	"fmt"
	"sync"
	"time"

	"vci/internal/domain"
)

type Store struct {
	mu          sync.Mutex
	head        int64
	issuers     map[string]domain.Issuer
	keys        map[string]domain.KeyVersion // by kid
	issuerOrder []string                     // issuer ids, in creation order
	creds       map[string]domain.Credential
	revoked     map[string]domain.Revocation // credentialID -> event
}

func New() *Store {
	return &Store{
		issuers: map[string]domain.Issuer{},
		keys:    map[string]domain.KeyVersion{},
		creds:   map[string]domain.Credential{},
		revoked: map[string]domain.Revocation{},
	}
}

func (s *Store) Migrate(ctx context.Context) error { return nil }
func (s *Store) Head(ctx context.Context) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.head, nil
}

func (s *Store) nextSnap() int64 {
	s.head++
	return s.head
}

func (s *Store) CreateIssuer(ctx context.Context, name string, pub, priv string, now time.Time) (domain.Issuer, domain.KeyVersion, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	snap := s.nextSnap()
	id := fmt.Sprintf("iss_%08d", len(s.issuerOrder)+1)
	iss := domain.Issuer{ID: id, Name: name, CreatedAt: now, Snapshot: snap}
	s.issuers[id] = iss
	s.issuerOrder = append(s.issuerOrder, id)
	key := domain.KeyVersion{
		ID: fmt.Sprintf("key_%s_1", id[4:]), IssuerID: id, Seq: 1,
		PublicKey: pub, PrivateKey: priv, Algorithm: "Ed25519",
		ValidFrom: now, Snapshot: snap,
	}
	s.keys[key.ID] = key
	return iss, key, nil
}

func (s *Store) RotateKey(ctx context.Context, issuerID string, pub, priv string, now time.Time, closeOld bool) (domain.KeyVersion, domain.KeyVersion, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ex := s.issuers[issuerID]; !ex {
		return domain.KeyVersion{}, domain.KeyVersion{}, domain.ErrIssuerNotFound
	}
	old, hadOpen := s.activeKeyLocked(issuerID)
	if hadOpen && closeOld {
		old.RetiredAt = &now
		s.keys[old.ID] = old
	}
	seq := 1
	if hadOpen {
		seq = old.Seq + 1
	} else {
		// No open version: continue from the highest historical seq.
		for _, k := range s.keys {
			if k.IssuerID == issuerID && k.Seq >= seq {
				seq = k.Seq + 1
			}
		}
	}
	snap := s.nextSnap()
	if hadOpen {
		old.Snapshot = snap
		s.keys[old.ID] = old
	}
	nk := domain.KeyVersion{
		ID: fmt.Sprintf("key_%s_%d", issuerID[4:], seq), IssuerID: issuerID,
		Seq: seq, PublicKey: pub, PrivateKey: priv, Algorithm: "Ed25519",
		ValidFrom: now, Snapshot: snap,
	}
	s.keys[nk.ID] = nk
	return nk, old, nil
}

func (s *Store) RetireActiveKey(ctx context.Context, issuerID string, now time.Time) (domain.KeyVersion, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.activeKeyLocked(issuerID)
	if !ok {
		if _, ex := s.issuers[issuerID]; !ex {
			return domain.KeyVersion{}, domain.ErrIssuerNotFound
		}
		return domain.KeyVersion{}, domain.ErrNoActiveKey
	}
	snap := s.nextSnap()
	k.RetiredAt = &now
	k.Snapshot = snap
	s.keys[k.ID] = k
	return k, nil
}

func (s *Store) GetIssuer(ctx context.Context, issuerID string) (domain.Issuer, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	iss, ok := s.issuers[issuerID]
	if !ok {
		return domain.Issuer{}, domain.ErrIssuerNotFound
	}
	return iss, nil
}

func (s *Store) ActiveKey(ctx context.Context, issuerID string) (domain.KeyVersion, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.activeKeyLocked(issuerID)
	if !ok {
		if _, ex := s.issuers[issuerID]; !ex {
			return domain.KeyVersion{}, domain.ErrIssuerNotFound
		}
		return domain.KeyVersion{}, domain.ErrNoActiveKey
	}
	return k, nil
}

// activeKeyLocked: highest seq with no RetiredAt and ValidFrom <= "now" is
// not modeled here on read (callers pass now through issuance); rotation
// closes old keys so there is at most one open version anyway.
func (s *Store) activeKeyLocked(issuerID string) (domain.KeyVersion, bool) {
	var best domain.KeyVersion
	found := false
	for _, k := range s.keys {
		if k.IssuerID != issuerID || k.RetiredAt != nil {
			continue
		}
		if !found || k.Seq > best.Seq {
			best, found = k, true
		}
	}
	return best, found
}

func (s *Store) IssueCredential(
	ctx context.Context,
	issuerID, subject, purpose string,
	notBefore, expiresAt, now time.Time,
	sign domain.SignFunc,
) (domain.Credential, domain.KeyVersion, error) {
	if !expiresAt.After(notBefore) {
		return domain.Credential{}, domain.KeyVersion{}, fmt.Errorf("%w: expires_at must be after not_before", domain.ErrConflict)
	}
	s.mu.Lock()
	k, ok := s.activeKeyLocked(issuerID)
	if !ok {
		s.mu.Unlock()
		if _, err := s.GetIssuer(ctx, issuerID); err != nil {
			return domain.Credential{}, domain.KeyVersion{}, err
		}
		return domain.Credential{}, domain.KeyVersion{}, domain.ErrNoActiveKey
	}
	payloadJSON, contentJSON, signature, hash, err := sign(k, now)
	if err != nil {
		s.mu.Unlock()
		return domain.Credential{}, domain.KeyVersion{}, err
	}
	snap := s.nextSnap()
	id := fmt.Sprintf("cred_%012d", len(s.creds)+1)
	c := domain.Credential{
		ID: id, IssuerID: issuerID, Subject: subject, Purpose: purpose,
		NotBefore: notBefore, ExpiresAt: expiresAt, ContentHash: hash,
		ContentJSON: contentJSON, PayloadJSON: payloadJSON, Signature: signature,
		KeyID: k.ID, IssuedAt: now, Snapshot: snap,
	}
	s.creds[id] = c
	s.mu.Unlock()
	return c, k, nil
}

func (s *Store) GetCredential(ctx context.Context, id string) (domain.Credential, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.creds[id]
	if !ok {
		return domain.Credential{}, domain.ErrNotFound
	}
	return c, nil
}

func (s *Store) Revoke(ctx context.Context, credentialID, reason string, effectiveAt, now time.Time) (domain.Revocation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.creds[credentialID]
	if !ok {
		return domain.Revocation{}, domain.ErrNotFound
	}
	if _, dup := s.revoked[c.ID]; dup {
		return domain.Revocation{}, fmt.Errorf("%w: credential already revoked", domain.ErrConflict)
	}
	snap := s.nextSnap()
	rev := domain.Revocation{
		ID:           fmt.Sprintf("rev_%012d", len(s.revoked)+1),
		CredentialID: credentialID, Reason: reason, EffectiveAt: effectiveAt,
		CreatedAt: now, Snapshot: snap,
	}
	s.revoked[credentialID] = rev
	return rev, nil
}

func (s *Store) ReadVerificationState(ctx context.Context, credentialID string) (domain.VerificationState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := domain.VerificationState{Snapshot: s.head}
	if c, ok := s.creds[credentialID]; ok {
		cc := c
		st.Credential = &cc
		if k, ok := s.keys[c.KeyID]; ok {
			kk := k
			st.Key = &kk
		}
		if rev, ok := s.revoked[credentialID]; ok {
			rr := rev
			st.Revocation = &rr
		}
	}
	return st, nil
}

// TestOnlyReplaceSignature mutates a credential signature in place. Test
// support only — the production schema is append-only.
func (s *Store) TestOnlyReplaceSignature(credentialID string, sig []byte) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.creds[credentialID]
	if !ok {
		return false
	}
	c.Signature = sig
	s.creds[credentialID] = c
	return true
}
