// Package service orchestrates evidence verification, OPA evaluation and
// immutable report persistence. It contains no HTTP code.
package service

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"mirror-admission/internal/crypto/digest"
	"mirror-admission/internal/model"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/verifier"
)

// ErrBadRequest is returned for malformed client input (HTTP 400).
var ErrBadRequest = errors.New("bad request")

type badRequest struct{ msg string }

func (e *badRequest) Error() string { return e.msg }
func (e *badRequest) Unwrap() error { return ErrBadRequest }

func badf(format string, args ...any) error {
	return &badRequest{msg: fmt.Sprintf(format, args...)}
}

// Store persists reports append-only.
type Store interface {
	Save(ctx context.Context, r *model.Report) error
	Get(ctx context.Context, id string) (*model.Report, error)
	List(ctx context.Context) ([]model.Report, error)
}

// Clock allows tests to pin boundary timestamps.
type Clock interface{ Now() time.Time }

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now().UTC() }

// Service ties the pieces together.
type Service struct {
	engine  *policy.Engine
	keys    verifier.Keys
	allowed policy.Allowlist
	store   Store
	clock   Clock
}

// New constructs a Service.
func New(engine *policy.Engine, keys verifier.Keys, allowed policy.Allowlist, store Store) *Service {
	return &Service{engine: engine, keys: keys, allowed: allowed, store: store, clock: systemClock{}}
}

// WithClock returns a copy using the given clock (tests).
func (s *Service) WithClock(c Clock) *Service {
	cp := *s
	cp.clock = c
	return &cp
}

// Meta describes the running frozen policy.
func (s *Service) Meta() model.PolicyMeta {
	return model.PolicyMeta{
		PolicyVersion:     s.engine.Version(),
		PolicySHA256:      s.engine.SHA256(),
		AllowlistVersion:  s.allowed.Version,
		AllowedBaseImages: append([]string(nil), s.allowed.AllowedBaseImages...),
		Rules:             []string{"no_root_user", "no_privileged", "base_image_allowed", "attestation_verified"},
	}
}

func newReportID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not a condition under which we fabricate ids.
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return "rpt_" + hex.EncodeToString(b[:])
}

// Evaluate is the core admission path.
func (s *Service) Evaluate(ctx context.Context, req model.AdmissionRequest) (*model.Report, error) {
	if len(req.ImageConfig) == 0 {
		return nil, badf("image_config is required")
	}
	if len(req.SBOM) == 0 {
		return nil, badf("sbom is required")
	}
	if req.AllowlistVersion == "" {
		return nil, badf("allowlist_version is required (pinned evaluation)")
	}
	if req.AllowlistVersion != s.allowed.Version {
		return nil, badf("allowlist_version %q does not match server frozen allowlist %q", req.AllowlistVersion, s.allowed.Version)
	}

	imageDigest, imgCfg, err := digest.ParseOCI(req.ImageConfig)
	if err != nil {
		return nil, badf("%v", err)
	}
	sbomDigest, sbom, err := digest.ParseSBOM(req.SBOM)
	if err != nil {
		return nil, badf("%v", err)
	}

	// Attestation: missing -> unknown; present-but-bad -> the policy gets a
	// verified=false record with the exact failure reason and rules it deny.
	var attPayload *model.VerifierPayload
	var attestedPayload *model.VerifierPayload // persisted in report only when authentic
	attValid := false
	attErr := ""
	if len(req.Attestation) > 0 {
		p, verr := verifier.VerifyAttestation(req.Attestation, s.keys, imageDigest, sbomDigest)
		if verr != nil {
			// An attestation WAS presented but failed: this is positive
			// evidence of an untrusted/failed result, hence a DENY rule
			// finding (not the "no evidence" UNKNOWN branch). Carry a
			// non-nil marker so Rego distinguishes the two.
			attErr = verr.Error()
			attPayload = &model.VerifierPayload{Kind: verifier.AttestationKind}
		} else {
			attPayload = p
			attestedPayload = p
			attValid = true
			// A tester result of "fail" is authentic evidence that the image
			// failed local checks: hand it to the rule as verified-but-failing.
			if p.Result == "fail" {
				attErr = fmt.Sprintf("verifier result is FAIL (%d checks reported)", len(p.Checks))
				attValid = false
			}
		}
	}

	// Exemptions: every presented envelope MUST verify. One bad waiver fails
	// the whole request — admission cannot ignore an invalid signature.
	exemptions := make([]*model.ExemptionPayload, 0, len(req.Exemptions))
	for i, raw := range req.Exemptions {
		ex, err := verifier.VerifyExemption(raw, s.keys)
		if err != nil {
			return nil, badf("exemptions[%d]: %v", i, err)
		}
		exemptions = append(exemptions, ex)
	}

	now := s.clock.Now().UTC()
	engineIn := policy.EngineInput{
		NowRFC3339:       now.Format(time.RFC3339Nano),
		ImageDigest:      imageDigest,
		ImageConfig:      imgCfg,
		SBOM:             sbom,
		SBOMDigest:       sbomDigest,
		Attestation:      attPayload,
		AttestationValid: attValid,
		AttestationError: attErr,
		Exemptions:       exemptions,
		Allowlist:        s.allowed,
	}

	res, err := s.engine.Evaluate(ctx, engineIn, now)
	if err != nil {
		return nil, fmt.Errorf("evaluate: %w", err)
	}

	report := &model.Report{
		ID:               newReportID(),
		CreatedAt:        now.Format(time.RFC3339Nano),
		Decision:         res.Decision,
		PolicyVersion:    s.engine.Version(),
		PolicySHA256:     s.engine.SHA256(),
		AllowlistVersion: s.allowed.Version,
		ImageDigest:      imageDigest,
		SBOMDigest:       sbomDigest,
		Findings:         res.Findings,
		UnusedExemptions: res.UnusedExemptions,
		Attestation:      attestedPayload,
	}
	if err := s.store.Save(ctx, report); err != nil {
		return nil, fmt.Errorf("persist report: %w", err)
	}
	return report, nil
}

// GetReport / ListReports read history. Re-evaluation never mutates these.
func (s *Service) GetReport(ctx context.Context, id string) (*model.Report, error) {
	r, err := s.store.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	return r, nil
}

func (s *Service) ListReports(ctx context.Context) ([]model.Report, error) {
	return s.store.List(ctx)
}

// DecodeStrict is shared by handlers/tests: unknown fields rejected.
func DecodeStrict(raw []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("unexpected trailing JSON value")
	}
	return nil
}
