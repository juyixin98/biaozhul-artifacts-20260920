// Package verify independently checks provenance chains.
//
// It never trusts the builder's report. For every action it reloads the
// signed record and recomputes, from the files currently on disk:
//
//   - the tool digest (project tree),
//   - every source digest (project tree),
//   - every upstream artifact digest against the upstream's own record,
//   - every output digest by rehashing the content-addressed cache blob,
//   - the record content id (file name binding) and HMAC signature,
//   - the input fingerprint over the recomputed input set.
//
// In addition it performs structural checks: unknown/missing records,
// edges to undeclared upstreams, and dependency cycles forged into records.
//
// Two different questions are answered separately:
//
//   - Complete provenance  (NodeResult.Complete): the signed chain from the
//     output back to sources/tools is intact.
//   - Reproducibility       (package repro): a clean rebuild produces the
//     same output digests. A chain can be complete yet not reproducible, and
//     a build can reproduce while its provenance is incomplete.
package verify

import (
	"fmt"
	"sort"
	"time"

	"bis/internal/digest"
	"bis/internal/provenance"
	"bis/internal/spec"
	"bis/internal/store"
)

// Code identifies one class of verification finding.
type Code string

const (
	// Structural
	CodeMissingRecord         Code = "MISSING_RECORD"          // index points at no record
	CodeRecordUnreadable      Code = "RECORD_UNREADABLE"       // file present but broken
	CodeUnknownAction         Code = "UNKNOWN_ACTION"          // record for an action not in the spec
	CodeUndeclaredUpstream    Code = "UNDECLARED_UPSTREAM"     // record edge not present in spec
	CodeMissingUpstream       Code = "MISSING_UPSTREAM"        // spec edge absent from record
	CodeMissingUpstreamRecord Code = "MISSING_UPSTREAM_RECORD" // declared upstream has no record
	CodeCycleDetected         Code = "CYCLE_DETECTED"          // cyclic dependency forged into records
	// Cryptographic / content
	CodeRecordHashMismatch     Code = "RECORD_HASH_MISMATCH" // body hash != record id
	CodeSignatureInvalid       Code = "SIGNATURE_INVALID"    // HMAC over body is wrong
	CodeSpecMismatch           Code = "SPEC_MISMATCH"        // record metadata disagrees with build.json
	CodeToolDigestMismatch     Code = "TOOL_DIGEST_MISMATCH" // tool file or invocation changed
	CodeSourceDigestMismatch   Code = "SOURCE_DIGEST_MISMATCH"
	CodeUpstreamDigestMismatch Code = "UPSTREAM_DIGEST_MISMATCH"
	CodeOutputDigestMismatch   Code = "OUTPUT_DIGEST_MISMATCH"
	CodeFingerprintMismatch    Code = "FINGERPRINT_MISMATCH"
	CodeBlobMissing            Code = "BLOB_MISSING" // referenced artifact absent from cache
	// Transitive
	CodeUpstreamChainBroken Code = "UPSTREAM_CHAIN_BROKEN" // an upstream is itself incomplete
)

// Finding is one verification observation.
type Finding struct {
	ActionID string `json:"action_id"`
	Code     Code   `json:"code"`
	Message  string `json:"message"`
}

// NodeResult is the verdict for one action.
type NodeResult struct {
	ActionID string `json:"action_id"`
	RecordID string `json:"record_id,omitempty"`
	Complete bool   `json:"complete"`
	Tainted  bool   `json:"tainted,omitempty"` // transitively depends on a broken node
	Codes    []Code `json:"codes,omitempty"`
}

// Report is the full verification verdict.
type Report struct {
	Project   string                 `json:"project"`
	CheckedAt string                 `json:"checked_at"`
	Complete  bool                   `json:"complete"`
	Nodes     map[string]*NodeResult `json:"nodes"`
	Findings  []Finding              `json:"findings"`
}

// Verifier verifies projects against a store.
type Verifier struct {
	st  *store.Store
	now func() time.Time
}

// New creates a Verifier.
func New(st *store.Store) *Verifier {
	return &Verifier{st: st, now: time.Now}
}

// Verify checks one project. A report is always returned (with findings);
// the error return is reserved for failures that prevent verification
// itself, such as a missing spec.
func (v *Verifier) Verify(p *spec.Project) (*Report, error) {
	rep := &Report{
		Project:   p.Name,
		CheckedAt: v.now().UTC().Format(time.RFC3339Nano),
		Nodes:     map[string]*NodeResult{},
	}
	add := func(actionID string, code Code, format string, args ...any) {
		rep.Findings = append(rep.Findings, Finding{
			ActionID: actionID, Code: code, Message: fmt.Sprintf(format, args...),
		})
	}

	idx := v.st.LoadIndex(p.Name)
	specActions := map[string]*spec.Action{}
	for i := range p.Actions {
		specActions[p.Actions[i].ID] = &p.Actions[i]
		rep.Nodes[p.Actions[i].ID] = &NodeResult{ActionID: p.Actions[i].ID}
	}

	// --- Load every referenced record. ---
	recs := map[string]*provenance.Record{}
	for i := range p.Actions {
		id := p.Actions[i].ID
		node := rep.Nodes[id]
		ent, ok := idx.Records[id]
		if !ok {
			add(id, CodeMissingRecord, "no provenance record indexed for action")
			continue
		}
		node.RecordID = ent.RecordID.Hex
		r, err := v.st.GetRecord(ent.RecordID)
		if err != nil {
			add(id, CodeRecordUnreadable, "%v", err)
			continue
		}
		recs[id] = r

		// File-name binding: the body must hash to its record id.
		cid, err := provenance.ContentID(r)
		if err != nil {
			add(id, CodeRecordHashMismatch, "%v", err)
		} else if !cid.Equal(ent.RecordID) {
			add(id, CodeRecordHashMismatch, "body hash %s != record id %s", cid.Hex[:12], ent.RecordID.Hex[:12])
		}
		// HMAC signature over the body.
		if err := r.VerifySig(v.st.Key()); err != nil {
			add(id, CodeSignatureInvalid, "%v", err)
		}
		if r.Project != p.Name {
			add(id, CodeSpecMismatch, "record project %q != %q", r.Project, p.Name)
		}
		if r.ActionID != id {
			add(id, CodeSpecMismatch, "record action id %q != %q", r.ActionID, id)
		}
	}

	// --- Structural graph checks over record edges. ---
	cycleNodes := v.detectCycles(p, recs, add)

	for i := range p.Actions {
		a := &p.Actions[i]
		r, present := recs[a.ID]
		if !present {
			continue
		}

		// Spec vs record: upstream edge sets.
		specUps := map[string]bool{}
		for _, up := range a.Upstream {
			specUps[up] = true
		}
		recUps := map[string]bool{}
		for _, u := range r.Upstreams {
			recUps[u.ActionID] = true
			if !specUps[u.ActionID] {
				add(a.ID, CodeUndeclaredUpstream, "record depends on %q, which is not declared in build.json", u.ActionID)
			}
		}
		for up := range specUps {
			if !recUps[up] {
				add(a.ID, CodeMissingUpstream, "record omits declared upstream %q", up)
			}
		}

		// Tool: path, args and digest recomputed from disk.
		toolPath, err := v.st.ResolveProjectPath(p.Name, a.Tool)
		if err != nil {
			add(a.ID, CodeToolDigestMismatch, "tool resolution: %v", err)
		} else {
			td, err := digest.OfFile(toolPath)
			if err != nil {
				add(a.ID, CodeToolDigestMismatch, "tool %q unreadable: %v", a.Tool, err)
			} else {
				if r.Tool.Path != a.Tool {
					add(a.ID, CodeToolDigestMismatch, "record tool path %q != declared %q", r.Tool.Path, a.Tool)
				}
				if !equalStrings(r.Tool.Args, a.Args) {
					add(a.ID, CodeToolDigestMismatch, "record args %v != declared args %v", r.Tool.Args, a.Args)
				}
				if !td.Equal(r.Tool.Digest) {
					add(a.ID, CodeToolDigestMismatch, "tool %q changed: disk %s != record %s",
						a.Tool, td.Hex[:12], r.Tool.Digest.Hex[:12])
				}
			}
		}

		// Sources: declared set and digests recomputed from disk.
		recSrc := map[string]digest.Digest{}
		for _, s := range r.Sources {
			recSrc[s.Path] = s.Digest
		}
		declSrc := map[string]bool{}
		for _, sp := range a.Sources {
			declSrc[sp] = true
			full, err := v.st.ResolveProjectPath(p.Name, sp)
			if err != nil {
				add(a.ID, CodeSourceDigestMismatch, "source %q unresolvable: %v", sp, err)
				continue
			}
			got, err := digest.OfFile(full)
			if err != nil {
				add(a.ID, CodeSourceDigestMismatch, "source %q unreadable: %v", sp, err)
				continue
			}
			want, ok := recSrc[sp]
			if !ok {
				add(a.ID, CodeSourceDigestMismatch, "record omits declared source %q (current %s)", sp, got.Hex[:12])
			} else if !got.Equal(want) {
				add(a.ID, CodeSourceDigestMismatch, "source %q changed: disk %s != record %s",
					sp, got.Hex[:12], want.Hex[:12])
			}
		}
		for sp := range recSrc {
			if !declSrc[sp] {
				add(a.ID, CodeSourceDigestMismatch, "record contains undeclared source %q", sp)
			}
		}

		// Upstreams: recorded digest must equal the upstream record's output.
		upOutputs := map[string]map[string]digest.Digest{} // action -> output -> digest
		for _, u := range r.Upstreams {
			ur, ok := recs[u.ActionID]
			if !ok {
				// The edge points at an action with no usable record. If the
				// spec declares it, this node cannot be complete: its input
				// artifact is unattested. Undeclared edges were already
				// reported as UNDECLARED_UPSTREAM.
				if specUps[u.ActionID] {
					add(a.ID, CodeMissingUpstreamRecord, "declared upstream %q has no verifiable record", u.ActionID)
				}
				continue
			}
			m := map[string]digest.Digest{}
			for _, o := range ur.Outputs {
				m[o.Path] = o.Digest
			}
			upOutputs[u.ActionID] = m
			want, ok := m[u.Output]
			if !ok {
				add(a.ID, CodeUpstreamDigestMismatch, "upstream %q has no recorded output %q", u.ActionID, u.Output)
				continue
			}
			if !want.Equal(u.Digest) {
				add(a.ID, CodeUpstreamDigestMismatch,
					"link to upstream %q output %q altered: link %s != upstream record %s",
					u.ActionID, u.Output, u.Digest.Hex[:12], want.Hex[:12])
			}
		}

		// Outputs: declared set, cache presence and independent rehash.
		recOut := map[string]provenance.OutputRef{}
		for _, o := range r.Outputs {
			recOut[o.Path] = o
		}
		for _, op := range a.Outputs {
			o, ok := recOut[op]
			if !ok {
				add(a.ID, CodeOutputDigestMismatch, "record omits declared output %q", op)
				continue
			}
			if !v.st.HasBlob(o.Digest) {
				add(a.ID, CodeBlobMissing, "output %q blob %s not in cache", op, o.Digest.Hex[:12])
				continue
			}
			got, err := digest.OfFile(v.st.BlobPath(o.Digest))
			if err != nil {
				add(a.ID, CodeOutputDigestMismatch, "output %q unreadable: %v", op, err)
				continue
			}
			if !got.Equal(o.Digest) {
				add(a.ID, CodeOutputDigestMismatch, "output %q content changed: disk %s != record %s",
					op, got.Hex[:12], o.Digest.Hex[:12])
			}
		}
		for op := range recOut {
			if !contains(a.Outputs, op) {
				add(a.ID, CodeOutputDigestMismatch, "record contains undeclared output %q", op)
			}
		}

		// Fingerprint: recompute from the *recomputed-on-disk* tool/sources
		// and the *recorded* upstream links (their integrity is checked
		// above). This catches edits to any input-reference field even if
		// they were re-signed by a holder of the key.
		toolRef := provenance.ToolRef{Path: a.Tool, Args: append([]string(nil), a.Args...), Digest: toolDigestSafe(v.st, p.Name, a.Tool)}
		srcs := make([]provenance.SourceRef, 0, len(a.Sources))
		for _, sp := range a.Sources {
			if full, err := v.st.ResolveProjectPath(p.Name, sp); err == nil {
				if d, err := digest.OfFile(full); err == nil {
					srcs = append(srcs, provenance.SourceRef{Path: sp, Digest: d})
				}
			}
		}
		ups := append([]provenance.UpstreamRef(nil), r.Upstreams...)
		fp := provenance.ComputeFingerprint(provenance.FingerprintInput{
			ProjectName: p.Name, ActionID: a.ID, Tool: toolRef, Sources: srcs, Upstreams: ups,
		})
		if !fp.Equal(r.InputFingerprint) {
			add(a.ID, CodeFingerprintMismatch, "recomputed input fingerprint %s != record %s",
				fp.Hex[:12], r.InputFingerprint.Hex[:12])
		}
	}

	// --- Per-node code aggregation. ---
	nodeCodes := map[string][]Code{}
	for _, f := range rep.Findings {
		if _, ok := rep.Nodes[f.ActionID]; ok {
			nodeCodes[f.ActionID] = append(nodeCodes[f.ActionID], f.Code)
		}
	}

	// --- Transitive completeness over the *recorded* graph. ---
	// A node's dependencies include every spec-declared upstream, even ones
	// with no record: such a node is never present in recs and therefore can
	// never be marked complete, blocking every transitive consumer.
	deps := map[string]map[string]bool{}
	for i := range p.Actions {
		a := &p.Actions[i]
		m := map[string]bool{}
		if r, ok := recs[a.ID]; ok {
			for _, u := range r.Upstreams {
				m[u.ActionID] = true
			}
		}
		for _, up := range a.Upstream {
			m[up] = true
		}
		deps[a.ID] = m
	}

	// Iterative fixpoint: a node is complete iff it has no local error
	// finding, is not on a cycle, and all its deps are complete.
	localOK := map[string]bool{}
	for id := range rep.Nodes {
		_, hasRec := recs[id]
		localOK[id] = hasRec && !cycleNodes[id] && !hasErrorCode(nodeCodes[id])
	}
	complete := map[string]bool{}
	for {
		changed := false
		for id, ok := range localOK {
			if !ok {
				continue
			}
			good := true
			for d := range deps[id] {
				if !complete[d] {
					good = false
				}
			}
			if good && !complete[id] {
				complete[id] = true
				changed = true
			}
		}
		if !changed {
			break
		}
	}

	allComplete := true
	ids := make([]string, 0, len(rep.Nodes))
	for id, n := range rep.Nodes {
		ids = append(ids, id)
		n.Complete = complete[id]
		if !n.Complete {
			allComplete = false
		}
		n.Codes = dedupSort(nodeCodes[id])
	}

	// Taint propagation: mark direct broken nodes and every transitive
	// consumer, then tag consumers that themselves have no local error with
	// UPSTREAM_CHAIN_BROKEN so the transitive nature is explicit.
	tainted := map[string]bool{}
	for id, n := range rep.Nodes {
		if !n.Complete && !cycleNodes[id] {
			// Seed only nodes broken on their own merits; cycle nodes are
			// already explicitly flagged.
			if recs[id] != nil && hasErrorCode(nodeCodes[id]) {
				tainted[id] = true
			} else if recs[id] == nil {
				tainted[id] = true
			}
		}
	}
	for _, id := range cycleNodeList(cycleNodes) {
		tainted[id] = true
	}
	consumers := reverseEdges(p, recs)
	work := append([]string(nil), mapKeys(tainted)...)
	for len(work) > 0 {
		id := work[0]
		work = work[1:]
		for c := range consumers[id] {
			if !tainted[c] {
				tainted[c] = true
				work = append(work, c)
				n := rep.Nodes[c]
				if n != nil && n.Complete == false && !containsCode(n.Codes, CodeCycleDetected) && !hasErrorCode(n.Codes) {
					// only attach when not already carrying a concrete code
					n.Codes = append(n.Codes, CodeUpstreamChainBroken)
					rep.Findings = append(rep.Findings, Finding{
						ActionID: c, Code: CodeUpstreamChainBroken,
						Message: fmt.Sprintf("provenance chain passes through broken upstream %q", id),
					})
				}
			}
		}
	}
	for _, id := range ids {
		rep.Nodes[id].Tainted = tainted[id]
		rep.Nodes[id].Codes = dedupSort(rep.Nodes[id].Codes)
	}
	rep.Complete = allComplete

	sort.Slice(rep.Findings, func(i, j int) bool {
		if rep.Findings[i].ActionID != rep.Findings[j].ActionID {
			return rep.Findings[i].ActionID < rep.Findings[j].ActionID
		}
		return rep.Findings[i].Code < rep.Findings[j].Code
	})
	return rep, nil
}

// detectCycles runs iterative DFS over recorded upstream edges and returns
// the set of actions participating in (or reaching into) a cycle.
func (v *Verifier) detectCycles(p *spec.Project, recs map[string]*provenance.Record, add func(string, Code, string, ...any)) map[string]bool {
	const unvisited, onStack, done = 0, 1, 2
	color := map[string]int{}
	cycle := map[string]bool{}

	var visit func(id string, stack []string)
	visit = func(id string, stack []string) {
		color[id] = onStack
		stack = append(stack, id)
		r := recs[id]
		if r != nil {
			seen := map[string]bool{}
			var ups []string
			for _, u := range r.Upstreams {
				if !seen[u.ActionID] {
					seen[u.ActionID] = true
					ups = append(ups, u.ActionID)
				}
			}
			sort.Strings(ups)
			for _, up := range ups {
				if _, known := recs[up]; !known {
					continue
				}
				switch color[up] {
				case unvisited:
					visit(up, stack)
				case onStack:
					// Found a back edge up -> ... -> up. Mark all nodes on
					// the cycle segment.
					inCycle := false
					for _, x := range stack {
						if x == up {
							inCycle = true
						}
						if inCycle {
							if !cycle[x] {
								add(x, CodeCycleDetected, "recorded dependency graph contains a forged cycle involving %q", up)
							}
							cycle[x] = true
						}
					}
				}
			}
		}
		color[id] = done
	}
	for i := range p.Actions {
		id := p.Actions[i].ID
		if color[id] == unvisited {
			visit(id, nil)
		}
	}
	return cycle
}

func reverseEdges(p *spec.Project, recs map[string]*provenance.Record) map[string]map[string]bool {
	rev := map[string]map[string]bool{}
	for id, r := range recs {
		seen := map[string]bool{}
		for _, u := range r.Upstreams {
			if seen[u.ActionID] {
				continue
			}
			seen[u.ActionID] = true
			if rev[u.ActionID] == nil {
				rev[u.ActionID] = map[string]bool{}
			}
			rev[u.ActionID][id] = true
		}
	}
	return rev
}

func hasErrorCode(codes []Code) bool {
	for _, c := range codes {
		if c == CodeUpstreamChainBroken {
			continue
		}
		return true
	}
	return false
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func containsCode(xs []Code, x Code) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func dedupSort(codes []Code) []Code {
	if len(codes) == 0 {
		return nil
	}
	set := map[Code]bool{}
	for _, c := range codes {
		set[c] = true
	}
	out := make([]Code, 0, len(set))
	for c := range set {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func mapKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func cycleNodeList(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// toolDigestSafe recomputes a tool digest, returning the zero digest on
// failure (the failure itself is already reported).
func toolDigestSafe(st *store.Store, project, tool string) digest.Digest {
	full, err := st.ResolveProjectPath(project, tool)
	if err != nil {
		return digest.Digest{}
	}
	d, err := digest.OfFile(full)
	if err != nil {
		return digest.Digest{}
	}
	return d
}
