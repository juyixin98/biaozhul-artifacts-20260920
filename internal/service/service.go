// Package service implements the build provenance application layer:
// tool/action registration, action execution into isolated work directories,
// attestation, independent verification, reproduction, and impact queries.
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"buildprovenance/internal/canonical"
	"buildprovenance/internal/executor"
	"buildprovenance/internal/provenance"
)

// Issue codes returned by verification.
const (
	CodeArtifactNotFound       = "ARTIFACT_NOT_FOUND"
	CodeArtifactBlobMissing    = "ARTIFACT_BLOB_MISSING"
	CodeArtifactDigestMismatch = "ARTIFACT_DIGEST_MISMATCH"
	CodeRecordNotFound         = "RECORD_NOT_FOUND"
	CodeRecordHashMismatch     = "RECORD_HASH_MISMATCH"
	CodeRecordSigInvalid       = "RECORD_SIG_INVALID"
	CodeLogChainBroken         = "LOG_CHAIN_BROKEN"
	CodeCycleDetected          = "CYCLE_DETECTED"
	CodeToolNotFound           = "TOOL_NOT_FOUND"
	CodeToolDigestMismatch     = "TOOL_DIGEST_MISMATCH"
	CodeSourceNotFound         = "SOURCE_NOT_FOUND"
	CodeInputBlobMissing       = "INPUT_BLOB_MISSING"
	CodeInputDigestMismatch    = "INPUT_DIGEST_MISMATCH"
	CodeUpstreamHashMismatch   = "UPSTREAM_RECORD_HASH_MISMATCH"
	CodeUpstreamRecordMismatch = "UPSTREAM_RECORD_MISMATCH"
	CodeRecordOutputMismatch   = "RECORD_OUTPUT_DIGEST_MISMATCH"
	CodeOutputSizeMismatch     = "OUTPUT_SIZE_MISMATCH"
)

// Issue is one finding from verification.
type Issue struct {
	Code       string `json:"code"`
	Message    string `json:"message"`
	ArtifactID string `json:"artifactId,omitempty"`
}

// Service wires stores, log, policy and execution together.
type Service struct {
	policy    *executor.Policy
	sources   *provenance.SourceRegistry
	artifacts *provenance.ArtifactRegistry
	log       *provenance.Log
	workRoot  string // scratch root; distinct from all CAS roots

	mu    sync.Mutex
	tools map[string]provenance.Tool

	now func() time.Time

	// onChange, when set, is invoked after state-changing operations
	// (tool registration, action execution). The host uses it to flush
	// lookup indexes promptly. Failures are logged by the callback.
	onChange func()
}

// SetOnChange installs a state-change callback.
func (s *Service) SetOnChange(fn func()) {
	s.onChange = fn
}

// Config holds construction parameters.
type Config struct {
	Policy    *executor.Policy
	Sources   *provenance.SourceRegistry
	Artifacts *provenance.ArtifactRegistry
	Log       *provenance.Log
	WorkRoot  string
}

// New creates a Service and ensures the work root exists.
func New(cfg Config) (*Service, error) {
	if err := os.MkdirAll(cfg.WorkRoot, 0o755); err != nil {
		return nil, fmt.Errorf("service: work root: %w", err)
	}
	return &Service{
		policy:    cfg.Policy,
		sources:   cfg.Sources,
		artifacts: cfg.Artifacts,
		log:       cfg.Log,
		workRoot:  cfg.WorkRoot,
		tools:     map[string]provenance.Tool{},
		now:       time.Now,
	}, nil
}

// ---- sources ---------------------------------------------------------------

// RegisterSource stores source content and returns metadata.
func (s *Service) RegisterSource(path string, content []byte) (provenance.Source, error) {
	src, err := s.sources.Register(path, content, s.now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return provenance.Source{}, err
	}
	s.notify()
	return src, nil
}

func (s *Service) GetSource(path string) (provenance.Source, error) { return s.sources.Get(path) }
func (s *Service) ListSources() []provenance.Source                 { return s.sources.List() }

// ---- tools -----------------------------------------------------------------

// RegisterTool validates and hashes a declared build action.
func (s *Service) RegisterTool(def provenance.ToolDefinition) (provenance.Tool, error) {
	if def.Name == "" {
		return provenance.Tool{}, errors.New("tool name required")
	}
	if len(def.Command) == 0 {
		return provenance.Tool{}, errors.New("tool command required")
	}
	rc, err := s.policy.Resolve(def.Command)
	if err != nil {
		return provenance.Tool{}, err
	}
	d, err := toolDigest(def.Name, def.Command, def.Env, rc)
	if err != nil {
		return provenance.Tool{}, err
	}
	s.mu.Lock()
	if existing, ok := s.tools[def.Name]; ok {
		s.mu.Unlock()
		if existing.Digest != d {
			return provenance.Tool{}, fmt.Errorf("tool %q already registered with a different digest (immutable); existing=%s new=%s",
				def.Name, existing.Digest.Short(), d.Short())
		}
		return existing, nil
	}
	t := provenance.Tool{
		Name:         def.Name,
		Command:      append([]string{}, def.Command...),
		Env:          cloneEnv(def.Env),
		Interpreter:  rc.Interpreter,
		Script:       rc.Script,
		Digest:       d,
		RegisteredAt: s.now().UTC().Format(time.RFC3339Nano),
	}
	s.tools[def.Name] = t
	s.mu.Unlock()
	s.notify()
	return t, nil
}

// GetTool returns a registered tool.
func (s *Service) GetTool(name string) (provenance.Tool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tools[name]
	if !ok {
		return provenance.Tool{}, fmt.Errorf("%w: tool %q", provenance.ErrNotFound, name)
	}
	return t, nil
}

// ListTools returns registered tools ordered by name.
func (s *Service) ListTools() []provenance.Tool {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]provenance.Tool, 0, len(s.tools))
	for _, t := range s.tools {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// RestoreTool inserts a tool from persisted state without re-hashing.
func (s *Service) RestoreTool(t provenance.Tool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tools[t.Name] = t
}

func toolDigest(name string, argv []string, env map[string]string, rc *executor.ResolvedCommand) (provenance.Digest, error) {
	m := map[string]any{
		"name":              name,
		"command":           argv,
		"env":               env,
		"interpreterDigest": string(rc.InterpreterDigest),
		"scriptDigest":      string(rc.ScriptDigest),
	}
	b, err := canonical.Encode(m)
	if err != nil {
		return "", err
	}
	return provenance.DigestBytes(b), nil
}

// rehashTool re-resolves a registered tool's files and recomputes its digest
// independently (used by execute/verify/reproduce).
func (s *Service) rehashTool(t provenance.Tool) (provenance.Digest, *executor.ResolvedCommand, error) {
	rc, err := s.policy.Resolve(t.Command)
	if err != nil {
		return "", nil, err
	}
	d, err := toolDigest(t.Name, t.Command, t.Env, rc)
	if err != nil {
		return "", nil, err
	}
	return d, rc, nil
}

// ---- action execution ------------------------------------------------------

// ActionResult is returned for a successful execution.
type ActionResult struct {
	ActionID  string                `json:"actionId"`
	Tool      string                `json:"tool"`
	Artifacts []provenance.Artifact `json:"artifacts"`
	Stdout    string                `json:"stdout"`
}

// ExecuteAction resolves bindings, checks for cycles in the upstream graph,
// runs the command in an isolated work directory, stores outputs in the
// artifact cache, and appends attestation records.
func (s *Service) ExecuteAction(ctx context.Context, req provenance.ActionRequest) (*ActionResult, error) {
	t, err := s.GetTool(req.Tool)
	if err != nil {
		return nil, err
	}
	currentDigest, rc, err := s.rehashTool(t)
	if err != nil {
		return nil, fmt.Errorf("tool resolution: %w", err)
	}
	if currentDigest != t.Digest {
		return nil, fmt.Errorf("tool %q digest drifted since registration: registered %s, now %s (refusing to run)",
			t.Name, t.Digest.Short(), currentDigest.Short())
	}

	if len(req.Outputs) == 0 {
		return nil, errors.New("at least one output is required")
	}
	seenSlot := map[string]bool{}
	inputs := make([]provenance.InputRef, 0, len(req.Inputs))
	staging := make([]executor.MaterializedInput, 0, len(req.Inputs))
	stageDir, err := os.MkdirTemp(s.workRoot, "stage-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(stageDir)

	for _, b := range req.Inputs {
		if err := executor.ValidateSlot(b.Slot); err != nil {
			return nil, err
		}
		if seenSlot[b.Slot] {
			return nil, fmt.Errorf("duplicate input slot %q", b.Slot)
		}
		seenSlot[b.Slot] = true
		if (b.SourcePath == "") == (b.ArtifactID == "") {
			return nil, fmt.Errorf("binding %q must set exactly one of sourcePath/artifactId", b.Slot)
		}
		var ref provenance.InputRef
		var stagePath string
		if b.SourcePath != "" {
			src, err := s.sources.Get(b.SourcePath)
			if err != nil {
				return nil, err
			}
			blob, err := s.sourcesCAS().Get(src.Digest)
			if err != nil {
				return nil, fmt.Errorf("source %q blob: %w", b.SourcePath, err)
			}
			ref = provenance.InputRef{Slot: b.Slot, Kind: "source", Path: src.Path, Digest: src.Digest}
			stagePath = filepath.Join(stageDir, b.Slot)
			if err := os.WriteFile(stagePath, blob, 0o644); err != nil {
				return nil, err
			}
		} else {
			art, err := s.artifacts.Get(b.ArtifactID)
			if err != nil {
				return nil, err
			}
			blob, err := s.artifacts.CAS().Get(art.Digest)
			if err != nil {
				return nil, fmt.Errorf("artifact %s blob: %w", b.ArtifactID, err)
			}
			ref = provenance.InputRef{Slot: b.Slot, Kind: "artifact", ArtifactID: art.ID, Digest: art.Digest}
			stagePath = filepath.Join(stageDir, b.Slot)
			if err := os.WriteFile(stagePath, blob, 0o644); err != nil {
				return nil, err
			}
		}
		// Independent check: hash the staged bytes rather than trusting index.
		staged, err := os.ReadFile(stagePath)
		if err != nil {
			return nil, err
		}
		if provenance.DigestBytes(staged) != ref.Digest {
			return nil, fmt.Errorf("internal: staged digest mismatch for slot %q", b.Slot)
		}
		inputs = append(inputs, ref)
		staging = append(staging, executor.MaterializedInput{
			Slot: ref.Slot, Path: stagePath, Digest: ref.Digest, Kind: ref.Kind,
			Ref: firstNonEmpty(ref.Path, ref.ArtifactID),
		})
	}

	outSlots := append([]string{}, req.Outputs...)
	sort.Strings(outSlots)
	for _, name := range outSlots {
		if err := executor.ValidateSlot(name); err != nil {
			return nil, fmt.Errorf("output slot: %w", err)
		}
	}
	actionID, err := computeActionID(t.Digest, inputs, outSlots)
	if err != nil {
		return nil, err
	}

	// Defense in depth: refuse to execute if the existing attestation graph
	// already contains a cycle reachable from the bound upstream artifacts.
	// Normal appends cannot create one (inputs must pre-exist); this catches
	// forged/tampered stores.
	for _, in := range inputs {
		if in.Kind == "artifact" {
			if cyc, err := s.findCycle(in.ArtifactID); err != nil {
				return nil, err
			} else if cyc != nil {
				return nil, fmt.Errorf("%w: upstream graph contains cycle %v", errCycle, cyc)
			}
		}
	}

	run, err := s.policy.Run(ctx, rc, executor.RunOptions{
		WorkRoot: s.workRoot,
		Env:      t.Env,
		Inputs:   staging,
		Outputs:  outSlots,
	})
	if err != nil {
		return nil, err
	}
	if !filepathHasPrefix(run.WorkDir, s.workRoot) {
		return nil, fmt.Errorf("internal: work dir escaped work root")
	}
	defer os.RemoveAll(run.WorkDir)

	sort.Slice(inputs, func(i, j int) bool { return inputs[i].Slot < inputs[j].Slot })

	res := &ActionResult{ActionID: actionID, Tool: t.Name}
	for _, out := range run.Outputs {
		content, err := os.ReadFile(out.Path)
		if err != nil {
			return nil, err
		}
		outDigest := provenance.DigestBytes(content)
		artID, err := computeArtifactID(t.Digest, inputs, out.Slot)
		if err != nil {
			return nil, err
		}
		if existing, gErr := s.artifacts.Get(artID); gErr == nil {
			if existing.Digest != outDigest {
				return nil, fmt.Errorf("non-deterministic action %s: artifact %s already recorded with digest %s but rerun produced %s; refusing to overwrite provenance",
					actionID, artID, existing.Digest.Short(), outDigest.Short())
			}
			res.Artifacts = append(res.Artifacts, existing)
			continue
		}
		art, err := s.artifacts.Put(artID, content, func(d provenance.Digest, size int64) provenance.Artifact {
			return provenance.Artifact{
				ID: artID, ActionID: actionID, Name: out.Slot,
				Digest: d, Size: size, CreatedAt: s.now().UTC().Format(time.RFC3339Nano),
			}
		})
		if err != nil {
			return nil, err
		}

		ups := map[string]string{}
		for _, in := range inputs {
			if in.Kind == "artifact" {
				ur, err := s.log.Get(in.ArtifactID)
				if err != nil {
					return nil, err
				}
				ups[in.ArtifactID] = ur.RecordHash
			}
		}
		rec := provenance.Record{
			Prev:       s.log.Tail(),
			ActionID:   actionID,
			ArtifactID: art.ID,
			ToolName:   t.Name,
			ToolDigest: t.Digest,
			Inputs:     append([]provenance.InputRef{}, inputs...),
			OutputName: art.Name,
			OutputSize: art.Size,
			CreatedAt:  art.CreatedAt,
			Upstreams:  ups,
		}
		// Bind the record to the actual output content digest.
		rec.OutputDigest = outDigest
		appended, err := s.log.Append(rec)
		if err != nil {
			return nil, err
		}
		art.RecordHash = appended.RecordHash
		if err := s.artifacts.Update(art); err != nil {
			return nil, err
		}
		res.Artifacts = append(res.Artifacts, art)
	}
	sort.Slice(res.Artifacts, func(i, j int) bool { return res.Artifacts[i].Name < res.Artifacts[j].Name })
	res.Stdout = run.Stdout
	s.notify()
	return res, nil
}

func (s *Service) notify() {
	if s.onChange != nil {
		s.onChange()
	}
}

var errCycle = errors.New("cycle detected")

func (s *Service) sourcesCAS() *provenance.CAS { return s.sources.CAS() }

func computeActionID(toolDigest provenance.Digest, inputs []provenance.InputRef, outputs []string) (string, error) {
	b, err := canonical.Encode(map[string]any{
		"toolDigest": string(toolDigest),
		"inputs":     inputsForFingerprint(inputs),
		"outputs":    outputs,
	})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return "act_" + hex.EncodeToString(sum[:])[:12], nil
}

func computeArtifactID(toolDigest provenance.Digest, inputs []provenance.InputRef, outputName string) (string, error) {
	b, err := canonical.Encode(map[string]any{
		"toolDigest": string(toolDigest),
		"inputs":     inputsForFingerprint(inputs),
		"output":     outputName,
	})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return "art_" + hex.EncodeToString(sum[:])[:16], nil
}

func inputsForFingerprint(inputs []provenance.InputRef) []map[string]string {
	out := make([]map[string]string, 0, len(inputs))
	for _, in := range inputs {
		m := map[string]string{"slot": in.Slot, "kind": in.Kind, "digest": string(in.Digest)}
		if in.Kind == "source" {
			m["path"] = in.Path
		} else {
			m["artifactId"] = in.ArtifactID
		}
		out = append(out, m)
	}
	return out
}

// ---- graph: cycle detection ------------------------------------------------

// findCycle returns a cycle path if one exists in the attestation upstream
// graph reachable from rootID.
func (s *Service) findCycle(rootID string) ([]string, error) {
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := map[string]int{}
	var stack []string

	var dfs func(id string) ([]string, error)
	dfs = func(id string) ([]string, error) {
		color[id] = gray
		stack = append(stack, id)
		rec, err := s.log.Get(id)
		if err != nil {
			if errors.Is(err, provenance.ErrNotFound) {
				return nil, nil // missing records are reported by verify, not cycle walk
			}
			return nil, err
		}
		up := make([]string, 0, len(rec.Upstreams))
		for k := range rec.Upstreams {
			up = append(up, k)
		}
		sort.Strings(up)
		for _, u := range up {
			switch color[u] {
			case gray:
				// Cycle: extract from first occurrence of u in stack.
				for i, v := range stack {
					if v == u {
						return append(append([]string{}, stack[i:]...), u), nil
					}
				}
			case white:
				if cyc, err := dfs(u); err != nil {
					return nil, err
				} else if cyc != nil {
					return cyc, nil
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[id] = black
		return nil, nil
	}
	return dfs(rootID)
}

// ---- helpers ---------------------------------------------------------------

func cloneEnv(env map[string]string) map[string]string {
	if len(env) == 0 {
		return nil
	}
	out := make(map[string]string, len(env))
	for k, v := range env {
		out[k] = v
	}
	return out
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func filepathHasPrefix(path, prefix string) bool {
	rel, err := filepath.Rel(prefix, path)
	if err != nil {
		return false
	}
	return rel != ".." && !filepath.IsAbs(rel)
}

// ensureJSONRoundTrip helper retained for tests/debug output.
func mustJSON(v any) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}
