// Package apply atomically applies a delta.Patch to an on-disk artifact.
//
// Safety properties enforced here:
//
//   - The old artifact is opened read-only and its content is never mutated.
//     The new content is built entirely in a sibling temp file and switched
//     into place with one rename(2), so a failure at ANY pre-rename stage
//     leaves the old artifact byte-for-byte and name-for-name usable.
//   - The patch binds both digests: the on-disk artifact must hash to
//     patch.OldSum (wrong-base rejection), and the reconstructed bytes must
//     hash to patch.NewSum before the switch (corrupt-output rejection).
//   - A free-space preflight refuses to start unless NewSize + slack bytes
//     are available in the target directory.
//   - A per-target flock serializes concurrent applies; a stale temp from a
//     previous hard crash is detected and removed under the lock.
//   - An apply interrupted AFTER the rename is idempotent: re-running finds
//     the target already at NewSum and reports already-applied.
package apply

import (
	"errors"
	"fmt"
	"io"
	"os"

	"deltaupdate/internal/delta"
)

// Result reports what an Apply did.
type Result struct {
	// Applied is false when the target already equaled NewSum (idempotent
	// re-application after an interrupted run).
	Applied   bool
	OldDigest string
	NewDigest string
}

// ErrAlreadyApplied is wrapped (use errors.Is) when the target is already at
// NewSum and no switch was performed.
var ErrAlreadyApplied = errors.New("apply: target already at newSum")

// AlreadyApplied reports whether err represents a successful prior apply.
func AlreadyApplied(err error) bool { return errors.Is(err, ErrAlreadyApplied) }

// faultWriter wraps the temp file and injects a failure/crash after a set
// number of bytes during stage write-delta.
type faultWriter struct {
	w              io.Writer
	fp             FaultPolicy
	written        int64
	threshold      int64 // FailAfterBytes; -1 = disabled
	thresholdFired bool
}

func (fw *faultWriter) Write(p []byte) (int, error) {
	// Fire as soon as crossing the threshold.
	if fw.threshold >= 0 && !fw.thresholdFired && fw.written+int64(len(p)) > fw.threshold {
		fw.thresholdFired = true
		fw.fp.crashAt(StageWriteDelta)
		if fw.fp.FailStage == StageWriteDelta {
			return 0, ErrInjected
		}
	}
	n, err := fw.w.Write(p)
	fw.written += int64(n)
	return n, err
}

// Apply applies p to targetPath. oldContent must read the current artifact
// (or be nil if the target does not exist and OldSize is 0). oldContent is
// never modified.
func Apply(p *delta.Patch, oldContent io.ReadSeeker, targetPath string, opts Options) (*Result, error) {
	ctx := opts.ctx()
	if err := p.Validate(); err != nil {
		return nil, err
	}
	if err := p.VerifyPatchSum(); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrCorruptPatch, err)
	}
	if targetPath == "" {
		return nil, errors.New("apply: empty target path")
	}

	dir := parentDir(targetPath)
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		return nil, fmt.Errorf("apply: target directory missing: %s", dir)
	}

	// Serialize applies to this target.
	lock, err := acquireTargetLock(lockPath(targetPath))
	if err != nil {
		return nil, err
	}
	defer lock.release()

	// Stage check-old: inspect the existing target's digest.
	existing, existingErr := os.Open(targetPath)
	var oldDigest string
	switch {
	case existingErr == nil:
		d, err := delta.DigestReader(existing)
		existing.Close()
		if err != nil {
			return nil, err
		}
		oldDigest = d
		if d == p.NewSum {
			// Idempotent recovery after a crash following rename. Clean any
			// stale temp left behind, then report already-applied.
			os.Remove(tempPath(targetPath))
			return &Result{Applied: false, OldDigest: d, NewDigest: d}, ErrAlreadyApplied
		}
		if d != p.OldSum {
			return nil, fmt.Errorf("%w: file=%s patch=%s", ErrWrongBase, d, p.OldSum)
		}
		// Re-open for reconstruction (previous handle was closed).
	case os.IsNotExist(existingErr):
		if p.OldSize != 0 {
			return nil, fmt.Errorf("%w: target missing but patch expects a %d-byte base", ErrWrongBase, p.OldSize)
		}
		oldDigest = p.OldSum // sha256 of empty
	default:
		return nil, existingErr
	}
	if err := opts.Fault.failAt(StageCheckOld); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageCheckOld)

	// Stage space: preflight free bytes in the target directory.
	avail, err := opts.spaceChecker().AvailableBytes(dir)
	if err != nil {
		return nil, err
	}
	need := p.NewSize + opts.slack()
	if avail < need {
		return nil, fmt.Errorf("%w: need %d bytes, available %d in %s", ErrNoSpace, need, avail, dir)
	}
	if err := opts.Fault.failAt(StageSpace); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageSpace)

	// Stage prepare: remove stale temp from a prior crash, create fresh one.
	if err := os.Remove(tempPath(targetPath)); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("apply: remove stale temp: %w", err)
	}
	tmp, err := os.OpenFile(tempPath(targetPath), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("apply: create temp: %w", err)
	}
	aborted := true
	defer func() {
		if aborted {
			tmp.Close()
			os.Remove(tempPath(targetPath))
		}
	}()
	if err := opts.Fault.failAt(StagePrepare); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StagePrepare)

	// Stage write-delta: reconstruct into temp.
	threshold := int64(-1)
	if opts.Fault.FailStage == StageWriteDelta || opts.Fault.CrashStage == StageWriteDelta {
		threshold = opts.Fault.FailAfterBytes
	}
	fw := &faultWriter{w: tmp, fp: opts.Fault, threshold: threshold}
	hasher := newStreamHasher()
	out := io.MultiWriter(fw, hasher)
	if err := reconstruct(ctx, p, oldContent, out); err != nil {
		return nil, err
	}

	// Stage sync.
	if err := tmp.Sync(); err != nil {
		return nil, fmt.Errorf("apply: fsync temp: %w", err)
	}
	if err := opts.Fault.failAt(StageSync); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageSync)

	// Stage verify-delta: temp content must equal bound NewSum.
	newDigest := hasher.hex()
	if newDigest != p.NewSum {
		return nil, fmt.Errorf("apply: reconstructed digest mismatch: got %s want %s (patch corrupt?)", newDigest, p.NewSum)
	}
	if err := tmp.Close(); err != nil {
		return nil, fmt.Errorf("apply: close temp: %w", err)
	}
	if err := opts.Fault.failAt(StageVerifyDelta); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageVerifyDelta)

	// Stage rename: the single atomic switch. Old artifact remains fully
	// available under targetPath up to this instant.
	if err := opts.Fault.failAt(StageRename); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageRename)
	if err := os.Rename(tempPath(targetPath), targetPath); err != nil {
		return nil, fmt.Errorf("apply: atomic rename: %w", err)
	}
	aborted = false
	opts.Fault.crashAt(StagePostRename)

	// Stage fsync-dir: persist the directory entry.
	if err := fsyncDir(dir); err != nil {
		return nil, err
	}
	if err := opts.Fault.failAt(StageFsyncDir); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageFsyncDir)

	// Stage verify-new: read the result back through its final name.
	f, err := os.Open(targetPath)
	if err != nil {
		return nil, err
	}
	finalDigest, err := delta.DigestReader(f)
	f.Close()
	if err != nil {
		return nil, err
	}
	if finalDigest != p.NewSum {
		return nil, fmt.Errorf("apply: post-rename digest mismatch: got %s want %s", finalDigest, p.NewSum)
	}
	if err := opts.Fault.failAt(StageVerifyNew); err != nil {
		return nil, err
	}
	opts.Fault.crashAt(StageVerifyNew)

	return &Result{Applied: true, OldDigest: oldDigest, NewDigest: finalDigest}, nil
}
