package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"

	"deltaupdate/internal/apply"
	"deltaupdate/internal/delta"
)

type simulatedSpace int64

func (s simulatedSpace) AvailableBytes(string) (int64, error) { return int64(s), nil }

// apply --target FILE --patch FILE
//
// Fault injection is driven exclusively through environment variables so the
// same command line serves both the happy path and crash fixtures:
//
//	DELTA_FAULT_STAGE / DELTA_CRASH_STAGE / DELTA_FAULT_AFTER_BYTES
//	DELTA_SIM_FREE_BYTES
func runApply(args []string) error {
	fs := flag.NewFlagSet("apply", flag.ContinueOnError)
	target := fs.String("target", "", "target artifact path")
	patchPath := fs.String("patch", "", "patch JSON file ('-' for stdin)")
	_ = fs.String("cache", "", "unused for apply; accepted for symmetry")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *target == "" || *patchPath == "" {
		return fmt.Errorf("usage: apply --target FILE --patch FILE")
	}

	var raw []byte
	var err error
	if *patchPath == "-" {
		raw, err = io.ReadAll(os.Stdin)
	} else {
		raw, err = os.ReadFile(*patchPath)
	}
	if err != nil {
		return err
	}
	p, err := delta.UnmarshalPatch(raw)
	if err != nil {
		return err
	}
	if err := p.Validate(); err != nil {
		return err
	}

	var oldR io.ReadSeeker
	if f, oerr := os.Open(*target); oerr == nil {
		defer f.Close()
		oldR = f
	} else if !os.IsNotExist(oerr) {
		return oerr
	}

	opts := apply.Options{Fault: apply.FaultFromEnv()}
	if v := os.Getenv("DELTA_SIM_FREE_BYTES"); v != "" {
		n, perr := strconv.ParseInt(v, 10, 64)
		if perr != nil {
			return fmt.Errorf("bad DELTA_SIM_FREE_BYTES: %w", perr)
		}
		opts.Space = simulatedSpace(n)
	}

	res, applyErr := apply.Apply(p, oldR, *target, opts)
	out := map[string]any{}
	if res != nil {
		out["applied"] = res.Applied
		out["oldDigest"] = res.OldDigest
		out["newDigest"] = res.NewDigest
	}
	if applyErr != nil {
		if apply.AlreadyApplied(applyErr) {
			out["applied"] = false
			out["alreadyApplied"] = true
			enc, _ := json.Marshal(out)
			fmt.Println(string(enc))
			return nil
		}
		out["error"] = applyErr.Error()
		enc, _ := json.Marshal(out)
		fmt.Fprintln(os.Stderr, string(enc))
		var ec int
		switch {
		case errors.Is(applyErr, apply.ErrWrongBase):
			ec = 10
		case errors.Is(applyErr, apply.ErrCorruptPatch):
			ec = 11
		case errors.Is(applyErr, apply.ErrNoSpace):
			ec = 12
		case errors.Is(applyErr, apply.ErrLocked):
			ec = 13
		case errors.Is(applyErr, apply.ErrInjected):
			ec = 14
		default:
			ec = 1
		}
		os.Exit(ec)
	}
	enc, _ := json.Marshal(out)
	fmt.Println(string(enc))
	return nil
}
