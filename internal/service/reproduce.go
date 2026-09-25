package service

import (
	"context"
	"fmt"
	"os"
	"sort"

	"buildprovenance/internal/executor"
	"buildprovenance/internal/provenance"
)

// OutputReproduction compares one output of a rerun with its attestation.
type OutputReproduction struct {
	Slot             string            `json:"slot"`
	RecordedDigest   provenance.Digest `json:"recordedDigest"`
	ReproducedDigest provenance.Digest `json:"reproducedDigest"`
	Match            bool              `json:"match"`
}

// ReproduceReport answers a different question from Verify:
//
//	Verify     -> is the provenance chain complete and untampered?
//	Reproduce  -> does re-running the declared action on the attested inputs
//	             actually produce the recorded bytes?
//
// A complete provenance chain does NOT imply reproducibility (e.g. an action
// that embeds wall-clock time is fully attested yet not reproducible).
type ReproduceReport struct {
	ArtifactID         string               `json:"artifactId"`
	ActionID           string               `json:"actionId"`
	ProvenanceComplete bool                 `json:"provenanceComplete"`
	Reproducible       bool                 `json:"reproducible"`
	Outputs            []OutputReproduction `json:"outputs"`
	Notes              []string             `json:"notes,omitempty"`
}

// Reproduce independently re-runs the action that produced artifactID.
// Inputs are staged fresh from CAS (re-hashed, not trusted), the tool is
// re-resolved and re-digested from files on disk, and outputs are compared to
// the recorded digests.
func (s *Service) Reproduce(ctx context.Context, artifactID string) (*ReproduceReport, error) {
	rep := &ReproduceReport{ArtifactID: artifactID}

	vr, err := s.Verify(artifactID)
	if err != nil {
		return nil, err
	}
	rep.ProvenanceComplete = vr.Complete
	if !vr.Complete {
		rep.Notes = append(rep.Notes, "provenance verification failed; reproduction proceeds for diagnostics but result cannot be trusted")
	}

	rec, err := s.log.Get(artifactID)
	if err != nil {
		return nil, err
	}
	rep.ActionID = rec.ActionID

	t, err := s.GetTool(rec.ToolName)
	if err != nil {
		return nil, err
	}
	d, rc, err := s.rehashTool(t)
	if err != nil {
		return nil, err
	}
	if d != rec.ToolDigest {
		rep.Notes = append(rep.Notes, fmt.Sprintf("tool digest drifted: recorded %s vs current %s", rec.ToolDigest.Short(), d.Short()))
	}

	// Re-stage every recorded input from CAS bytes and re-hash on the way.
	stageDir, err := os.MkdirTemp(s.workRoot, "repro-stage-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(stageDir)
	mat := make([]executor.MaterializedInput, 0, len(rec.Inputs))
	inputs := append([]provenance.InputRef{}, rec.Inputs...)
	sort.Slice(inputs, func(i, j int) bool { return inputs[i].Slot < inputs[j].Slot })
	for _, in := range inputs {
		var blob []byte
		switch in.Kind {
		case "source":
			src, gErr := s.sources.Get(in.Path)
			if gErr != nil {
				return nil, gErr
			}
			blob, err = s.sourcesCAS().Get(src.Digest)
		case "artifact":
			art, gErr := s.artifacts.Get(in.ArtifactID)
			if gErr != nil {
				return nil, gErr
			}
			blob, err = s.artifacts.CAS().Get(art.Digest)
		default:
			return nil, fmt.Errorf("unknown input kind %q", in.Kind)
		}
		if err != nil {
			return nil, err
		}
		if got := provenance.DigestBytes(blob); got != in.Digest {
			return nil, fmt.Errorf("input %s: re-hashed digest %s != recorded %s; refusing to reproduce on tampered inputs", in.Slot, got, in.Digest)
		}
		sp := fmt.Sprintf("%s/%s", stageDir, in.Slot)
		if err := os.WriteFile(sp, blob, 0o644); err != nil {
			return nil, err
		}
		mat = append(mat, executor.MaterializedInput{Slot: in.Slot, Path: sp, Digest: in.Digest, Kind: in.Kind})
	}

	// Collect all output slots that belong to this action (one action can
	// emit several outputs, each with its own record).
	all := s.log.All()
	slotSet := map[string]bool{}
	for _, r := range all {
		if r.ActionID == rec.ActionID {
			slotSet[r.OutputName] = true
		}
	}
	slots := make([]string, 0, len(slotSet))
	for name := range slotSet {
		slots = append(slots, name)
	}
	sort.Strings(slots)

	run, runErr := s.policy.Run(ctx, rc, executor.RunOptions{
		WorkRoot: s.workRoot,
		Env:      t.Env,
		Inputs:   mat,
		Outputs:  slots,
	})
	if runErr != nil {
		if run != nil {
			defer os.RemoveAll(run.WorkDir)
		}
		rep.Notes = append(rep.Notes, fmt.Sprintf("re-execution failed: %v", runErr))
		rep.Reproducible = false
		return rep, nil
	}
	defer os.RemoveAll(run.WorkDir)

	bySlot := map[string]executor.Output{}
	for _, o := range run.Outputs {
		bySlot[o.Slot] = o
	}
	rep.Reproducible = true
	for _, slot := range slots {
		o := OutputReproduction{Slot: slot}
		// recorded digest: sibling record
		var recorded provenance.Digest
		for _, r := range all {
			if r.ActionID == rec.ActionID && r.OutputName == slot {
				recorded = r.OutputDigest
			}
		}
		o.RecordedDigest = recorded
		if out, ok := bySlot[slot]; ok {
			b, rErr := os.ReadFile(out.Path)
			if rErr != nil {
				rep.Notes = append(rep.Notes, rErr.Error())
				rep.Reproducible = false
				continue
			}
			o.ReproducedDigest = provenance.DigestBytes(b)
			o.Match = o.ReproducedDigest == recorded
			if !o.Match {
				rep.Reproducible = false
			}
		} else {
			rep.Reproducible = false
			rep.Notes = append(rep.Notes, fmt.Sprintf("output %s not produced on rerun", slot))
		}
		rep.Outputs = append(rep.Outputs, o)
	}
	sort.Slice(rep.Outputs, func(i, j int) bool { return rep.Outputs[i].Slot < rep.Outputs[j].Slot })
	return rep, nil
}
