// Package repro tests build-result reproducibility.
//
// This is intentionally separate from provenance verification (package
// verify): a complete signed chain says "this output really came from these
// inputs via this tool"; reproducibility says "running the same inputs and
// tool again produces byte-identical output". Neither implies the other.
//
// A reproduction run executes the project inside a throwaway store root
// (fresh empty cache, fresh work tree), then compares every freshly produced
// output digest with the digest recorded in the original provenance.
package repro

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"bis/internal/builder"
	"bis/internal/provenance"
	"bis/internal/spec"
	"bis/internal/store"
)

// ActionResult is the per-action reproducibility verdict.
type ActionResult struct {
	ActionID string             `json:"action_id"`
	Status   string             `json:"status"` // "reproduced" | "different" | "failed"
	Outputs  []OutputComparison `json:"outputs,omitempty"`
	Error    string             `json:"error,omitempty"`
}

// OutputComparison compares one output across the two runs.
type OutputComparison struct {
	Output         string `json:"output"`
	OriginalDigest string `json:"original_digest"`
	RerunDigest    string `json:"rerun_digest"`
	Reproduced     bool   `json:"reproduced"`
}

// Report is the reproducibility verdict.
type Report struct {
	Project     string          `json:"project"`
	Reproduced  bool            `json:"reproduced"`
	Status      string          `json:"status"` // "ok" | "failed"
	Rerun       *builder.Report `json:"rerun"`
	Actions     []ActionResult  `json:"actions"`
	CommandsRun []string        `json:"commands_run"`
	StartedAt   string          `json:"started_at"`
	FinishedAt  string          `json:"finished_at"`
}

// Run reproduces the project in an isolated store under scratchParent
// (typically the main store's root). The throwaway root is removed when
// cleanup is called.
func Run(main *store.Store, p *spec.Project, timeout time.Duration, now func() time.Time) (*Report, func(), error) {
	started := now().UTC().Format(time.RFC3339Nano)
	tag := now().UTC().Format("20060102T150405.000000000")
	tmpRoot := filepath.Join(filepath.Dir(main.WorkDir), "repro", "rerun-"+tag)
	if err := os.MkdirAll(tmpRoot, 0o755); err != nil {
		return nil, func() {}, err
	}
	cleanup := func() { _ = os.RemoveAll(tmpRoot) }

	// Link the project tree into the isolated store read-only by reference:
	// the fixture executes from a symlinked path, but to guarantee identical
	// tool/source contents, copy it instead.
	scratch, err := store.Open(tmpRoot)
	if err != nil {
		cleanup()
		return nil, func() {}, err
	}
	srcProj := main.ProjectRoot(p.Name)
	dstProj := scratch.ProjectRoot(p.Name)
	if err := copyTree(dstProj, srcProj); err != nil {
		cleanup()
		return nil, func() {}, err
	}

	sp, err := spec.Load(dstProj)
	if err != nil {
		cleanup()
		return nil, func() {}, err
	}

	b := builder.New(scratch, builder.WithTimeout(timeout))
	rerun, buildErr := b.Build(sp)

	// Original records for comparison.
	origIdx := main.LoadIndex(p.Name)
	origRecs := map[string]*provenance.Record{}
	for _, id := range origIdx.Records {
		if r, err := main.GetRecord(id.RecordID); err == nil {
			origRecs[r.ActionID] = r
		}
	}

	// Rerun records.
	newIdx := scratch.LoadIndex(sp.Name)
	rep := &Report{
		Project: p.Name, Rerun: rerun, StartedAt: started,
		FinishedAt: now().UTC().Format(time.RFC3339Nano),
	}
	if rerun != nil {
		rep.CommandsRun = rerun.CommandsRun
	}

	allRepro := true
	anyBuildFailure := buildErr != nil
	for i := range p.Actions {
		a := &p.Actions[i]
		ar := ActionResult{ActionID: a.ID}
		orig := origRecs[a.ID]
		newEnt, hasNew := newIdx.Records[a.ID]
		newRec := (*provenance.Record)(nil)
		if hasNew {
			newRec, _ = scratch.GetRecord(newEnt.RecordID)
		}
		switch {
		case newRec == nil:
			ar.Status = "failed"
			ar.Error = "rerun produced no record"
			allRepro = false
		case orig == nil:
			ar.Status = "failed"
			ar.Error = "no original record to compare against"
			allRepro = false
		default:
			ar.Status = "reproduced"
			om := map[string]provenance.OutputRef{}
			for _, o := range orig.Outputs {
				om[o.Path] = o
			}
			for _, no := range newRec.Outputs {
				cmp := OutputComparison{
					Output:      no.Path,
					RerunDigest: no.Digest.String(),
				}
				if oo, ok := om[no.Path]; ok {
					cmp.OriginalDigest = oo.Digest.String()
					cmp.Reproduced = oo.Digest.Equal(no.Digest)
				}
				if !cmp.Reproduced {
					ar.Status = "different"
					allRepro = false
				}
				ar.Outputs = append(ar.Outputs, cmp)
			}
		}
		rep.Actions = append(rep.Actions, ar)
	}
	rep.Reproduced = allRepro
	if anyBuildFailure {
		rep.Status = "failed"
	} else {
		rep.Status = "ok"
	}
	return rep, cleanup, nil
}

func copyTree(dst, src string) error {
	return filepath.Walk(src, func(path string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		switch {
		case fi.IsDir():
			return os.MkdirAll(target, fi.Mode().Perm())
		case fi.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		case fi.Mode().IsRegular():
			in, err := os.Open(path)
			if err != nil {
				return err
			}
			defer in.Close()
			out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, fi.Mode().Perm())
			if err != nil {
				return fmt.Errorf("copy %s: %w", rel, err)
			}
			if _, err := io.Copy(out, in); err != nil {
				out.Close()
				return err
			}
			return out.Close()
		default:
			return nil
		}
	})
}
