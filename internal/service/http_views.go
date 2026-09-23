package service

import (
	"context"
	"encoding/json"
	"time"
)

// RevokeInput is the HTTP-facing alias of RevokeRequest (named distinctly
// so handlers and service internals do not import each other's types).
type RevokeInput = RevokeRequest

// VerifyInput is the HTTP-facing verify request.
type VerifyInput struct {
	CredentialID    string
	ExpectedPurpose string
	AsOf            *time.Time
	Content         json.RawMessage
}

// VerifyHTTP runs verification and, when a content body is supplied,
// additionally compares its digest against the credential's content_hash.
func (s *Service) VerifyHTTP(ctx context.Context, in VerifyInput) (VerifyResult, error) {
	return s.VerifyCredentialByContent(ctx, VerifyRequest{
		CredentialID:    in.CredentialID,
		ExpectedPurpose: in.ExpectedPurpose,
		AsOf:            in.AsOf,
	}, in.Content)
}

// GetIssuerHTTP returns an issuer view for the read endpoint.
func (s *Service) GetIssuerHTTP(ctx context.Context, issuerID string) (IssuerView, error) {
	iss, err := s.store.GetIssuer(ctx, issuerID)
	if err != nil {
		return IssuerView{}, err
	}
	return IssuerView{
		ID: iss.ID, Name: iss.Name, CreatedAt: rfc(iss.CreatedAt), Snapshot: iss.Snapshot,
	}, nil
}

// GetCredentialHTTP returns the credential envelope for the read endpoint.
func (s *Service) GetCredentialHTTP(ctx context.Context, credentialID string) (CredentialView, error) {
	c, err := s.store.GetCredential(ctx, credentialID)
	if err != nil {
		return CredentialView{}, err
	}
	return credentialView(c, c.ContentJSON), nil
}
