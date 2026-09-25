package apply

import (
	"context"
	"errors"
	"os"
	"strconv"
)

// Application stages. A patch becomes visible to readers only at rename; every
// stage before that can fail or be interrupted while the old artifact stays
// byte-for-byte and name-for-name intact.
const (
	StageCheckOld    = "check-old"    // digest & bind-verify the existing artifact
	StageSpace       = "space"        // free-space preflight
	StagePrepare     = "prepare"      // clear stale temps, create new temp
	StageWriteDelta  = "write-delta"  // stream reconstructed bytes to temp
	StageSync        = "sync"         // fsync temp
	StageVerifyDelta = "verify-delta" // hash temp, must equal NewSum
	StageRename      = "rename"       // atomic temp -> target switch
	StagePostRename  = "post-rename"  // immediately after switch (crash tests)
	StageFsyncDir    = "fsync-dir"    // fsync parent directory
	StageVerifyNew   = "verify-new"   // final hash of target
)

// CrashExitCode is the process exit code used when a simulated crash fires.
const CrashExitCode = 42

var (
	// ErrWrongBase means the on-disk artifact's digest differs from the
	// patch's bound OldSum.
	ErrWrongBase = errors.New("apply: wrong base artifact: digest does not match patch oldSum")
	// ErrCorruptPatch means the patch's own integrity check failed.
	ErrCorruptPatch = errors.New("apply: corrupt patch: patchSum mismatch")
	// ErrNoSpace means the free-space preflight refused to start.
	ErrNoSpace = errors.New("apply: insufficient free space")
	// ErrLocked means another apply holds the target lock.
	ErrLocked = errors.New("apply: target is locked by another process")
	// ErrInjected is returned when a test fault policy triggers a failure.
	ErrInjected = errors.New("apply: injected failure")
)

// FaultPolicy drives failure/crash injection used by the acceptance fixtures.
// Zero value = no injection.
type FaultPolicy struct {
	// FailStage, when set to a stage name, makes Apply return ErrInjected at
	// that stage (a recoverable, in-process failure).
	FailStage string
	// CrashStage, when set, makes the process os.Exit(CrashExitCode) at that
	// stage (a hard crash; temp files and locks are deliberately left behind).
	CrashStage string
	// FailAfterBytes, with FailStage or CrashStage == StageWriteDelta, fires
	// the fault after this many output bytes have been written.
	FailAfterBytes int64
}

// SpaceChecker reports usable bytes in a directory. The production
// implementation uses statfs; tests inject a simulated value.
type SpaceChecker interface {
	AvailableBytes(dir string) (int64, error)
}

// Options controls Apply.
type Options struct {
	Fault   FaultPolicy
	Space   SpaceChecker // nil => real filesystem statfs
	Slack   int64        // extra bytes required beyond NewSize; default 64 KiB
	Context context.Context
}

func (o Options) ctx() context.Context {
	if o.Context != nil {
		return o.Context
	}
	return context.Background()
}

func (o Options) slack() int64 {
	if o.Slack > 0 {
		return o.Slack
	}
	return 64 << 10
}

func (o Options) spaceChecker() SpaceChecker {
	if o.Space != nil {
		return o.Space
	}
	return statfsChecker{}
}

// failAt returns ErrInjected when the recoverable-fault stage is reached.
func (f FaultPolicy) failAt(stage string) error {
	if f.FailStage == stage {
		return ErrInjected
	}
	return nil
}

// crashAt hard-exits when the crash stage is reached.
func (f FaultPolicy) crashAt(stage string) {
	if f.CrashStage == stage {
		os.Exit(CrashExitCode)
	}
}

// FaultFromEnv builds a FaultPolicy from the CLI fixture environment:
//
//	DELTA_FAULT_STAGE        fail stage (recoverable)
//	DELTA_CRASH_STAGE        crash stage (hard exit)
//	DELTA_FAULT_AFTER_BYTES  byte threshold for write-delta
func FaultFromEnv() FaultPolicy {
	fp := FaultPolicy{
		FailStage:  os.Getenv("DELTA_FAULT_STAGE"),
		CrashStage: os.Getenv("DELTA_CRASH_STAGE"),
	}
	if v := os.Getenv("DELTA_FAULT_AFTER_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err == nil && n >= 0 {
			fp.FailAfterBytes = n
		}
	}
	return fp
}
