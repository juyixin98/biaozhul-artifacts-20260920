// Command policylock regenerates the frozen policy hash lock. This is an
// deliberate release action: the server refuses to start when the lock does
// not match, so operators must consciously re-freeze after a policy review.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"mirror-admission/internal/policy"
)

func main() {
	policyPath := flag.String("policy", "policy/admission.rego", "policy path")
	out := flag.String("out", "policy/policy_freeze.lock.json", "lock output path")
	flag.Parse()

	b, err := os.ReadFile(*policyPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	lock := policy.FreezeLock{
		PolicyVersion: policy.PolicyVersion,
		PolicyPath:    *policyPath,
		SHA256:        policy.ComputeSHA256(b),
		RegoPackage:   "mirrorad.admission",
	}
	enc, err := json.MarshalIndent(lock, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.WriteFile(*out, append(enc, '\n'), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("froze policy %s at version %s sha256=%s\n", *policyPath, lock.PolicyVersion, lock.SHA256)
}
