package policy

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"github.com/open-policy-agent/opa/ast"
	"github.com/open-policy-agent/opa/rego"

	"mirror-admission/internal/model"
)

// ruleOrder defines the display order of findings in every report.
var ruleOrder = []string{
	"no_root_user",
	"no_privileged",
	"base_image_allowed",
	"attestation_verified",
}

// Allowlist is loaded at startup and version-pinned per request.
type Allowlist struct {
	Version           string   `json:"version"`
	AllowedBaseImages []string `json:"allowed_base_images"`
}

// EngineInput is everything Rego sees. It contains no Go-side "default
// true" values: attestation_valid is false unless a real signature check
// succeeded, and missing evidence arrives as null.
type EngineInput struct {
	NowRFC3339       string                    `json:"now_rfc3339"`
	ImageDigest      string                    `json:"image_digest"`
	ImageConfig      map[string]any            `json:"image_config"`
	SBOM             map[string]any            `json:"sbom"`
	SBOMDigest       string                    `json:"sbom_digest"`
	Attestation      *model.VerifierPayload    `json:"attestation"`
	AttestationValid bool                      `json:"attestation_valid"`
	AttestationError string                    `json:"attestation_error"`
	Exemptions       []*model.ExemptionPayload `json:"exemptions"`
	Allowlist        Allowlist                 `json:"allowlist"`
}

// Engine is a compiled, immutable policy instance.
type Engine struct {
	query    rego.PreparedEvalQuery
	version  string
	sha256   string
	compiled time.Time
}

// NewEngine compiles the Rego module with a strict compiler. Any compile
// error aborts startup — the server never serves an uncompilable policy.
func NewEngine(moduleBytes []byte, hash string) (*Engine, error) {
	compiler := ast.NewCompiler()
	compiler.SetErrorLimit(0) // surface every error
	r := rego.New(
		rego.Query("data.mirrorad.admission"),
		rego.Module("admission.rego", string(moduleBytes)),
		rego.Compiler(compiler),
	)
	pq, err := r.PrepareForEval(context.Background())
	if err != nil {
		return nil, fmt.Errorf("compile frozen policy: %w", err)
	}
	e := &Engine{query: pq, version: PolicyVersion, sha256: hash, compiled: time.Now().UTC()}

	// Self-check: the compiled module's declared version must equal the
	// engine's frozen version constant.
	var probe map[string]any
	if err := e.evalEmpty(&probe); err != nil {
		return nil, fmt.Errorf("policy version self-check: %w", err)
	}
	if v, _ := probe["version"].(string); v != PolicyVersion {
		return nil, fmt.Errorf("policy declares version %q but engine is frozen at %q", v, PolicyVersion)
	}
	return e, nil
}

func (e *Engine) evalEmpty(out *map[string]any) error {
	rs, err := e.query.Eval(context.Background(), rego.EvalInput(map[string]any{}))
	if err != nil {
		return err
	}
	if len(rs) == 0 {
		return fmt.Errorf("policy produced no result")
	}
	m, ok := rs[0].Expressions[0].Value.(map[string]any)
	if !ok {
		return fmt.Errorf("policy result is not an object")
	}
	*out = m
	return nil
}

// Version / SHA256 identify the frozen policy in reports.
func (e *Engine) Version() string { return e.version }
func (e *Engine) SHA256() string  { return e.sha256 }

// Result is the decoded Rego output.
type Result struct {
	Decision         string
	Findings         []model.Finding
	UnusedExemptions []model.UnusedExemption
}

// Evaluate runs the decision for one request at time now.
func (e *Engine) Evaluate(ctx context.Context, in EngineInput, now time.Time) (Result, error) {
	if in.NowRFC3339 == "" {
		in.NowRFC3339 = now.UTC().Format(time.RFC3339)
	}
	// Rego needs stable JSON-ish values; marshal through JSON once to avoid
	// any interface type surprise (e.g. nil pointer vs null).
	rawIn, err := json.Marshal(in)
	if err != nil {
		return Result{}, err
	}
	var inputValue any
	if err := json.Unmarshal(rawIn, &inputValue); err != nil {
		return Result{}, err
	}

	rs, err := e.query.Eval(ctx, rego.EvalInput(inputValue))
	if err != nil {
		return Result{}, fmt.Errorf("policy evaluation: %w", err)
	}
	if len(rs) == 0 || rs[0].Expressions == nil {
		return Result{}, fmt.Errorf("policy produced no result for input")
	}
	out, ok := rs[0].Expressions[0].Value.(map[string]any)
	if !ok {
		return Result{}, fmt.Errorf("policy result is not an object")
	}

	decision, _ := out["decision"].(string)
	if decision == "" {
		return Result{}, fmt.Errorf("policy returned empty decision (a rule likely failed to produce a finding)")
	}

	res := Result{Decision: decision}
	if res.Findings, err = decodeFindings(out["adjusted_findings"]); err != nil {
		return Result{}, err
	}
	res.UnusedExemptions, err = decodeUnused(out["unused_exemptions"], out["live_unconsumed"])
	if err != nil {
		return Result{}, err
	}
	return res, nil
}

func asMapSlice(v any) ([]map[string]any, error) {
	arr, ok := v.([]any)
	if !ok {
		if v == nil {
			return nil, nil
		}
		return nil, fmt.Errorf("expected array, got %T", v)
	}
	out := make([]map[string]any, 0, len(arr))
	for _, item := range arr {
		m, ok := item.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("finding is not an object: %T", item)
		}
		out = append(out, m)
	}
	return out, nil
}

func decodeFindings(v any) ([]model.Finding, error) {
	rows, err := asMapSlice(v)
	if err != nil {
		return nil, fmt.Errorf("decode findings: %w", err)
	}
	byRule := make(map[string]model.Finding, len(rows))
	for _, row := range rows {
		rule, _ := row["rule"].(string)
		status, _ := row["status"].(string)
		reason, _ := row["reason"].(string)
		if rule == "" || status == "" {
			return nil, fmt.Errorf("finding missing rule/status: %v", row)
		}
		f := model.Finding{Rule: rule, Status: status, Reason: reason}
		if exs, ok := row["exemptions"].([]any); ok {
			for _, x := range exs {
				if s, ok := x.(string); ok {
					f.Exemptions = append(f.Exemptions, s)
				}
			}
		}
		if f.Exemptions == nil {
			f.Exemptions = []string{}
		}
		byRule[rule] = f
	}
	// Enforce completeness: every frozen rule MUST have exactly one finding.
	// Missing evidence must be reported unknown, never silently dropped.
	out := make([]model.Finding, 0, len(ruleOrder))
	for _, r := range ruleOrder {
		f, ok := byRule[r]
		if !ok {
			return nil, fmt.Errorf("policy returned no finding for rule %q", r)
		}
		out = append(out, f)
	}
	return out, nil
}

func decodeUnused(parts ...any) ([]model.UnusedExemption, error) {
	var out []model.UnusedExemption
	for _, part := range parts {
		rows, err := asMapSlice(part)
		if err != nil {
			return nil, fmt.Errorf("decode exemptions: %w", err)
		}
		for _, row := range rows {
			id, _ := row["id"].(string)
			rule, _ := row["rule"].(string)
			reason, _ := row["reason"].(string)
			out = append(out, model.UnusedExemption{ID: id, Rule: rule, Reason: reason})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}
