package gc

import "fmt"

// fmtErrf annotates an error with the replica URL that caused it.
func fmtErrf(replicas []string, i int, format string, args ...any) error {
	name := fmt.Sprintf("replica#%d", i)
	if i < len(replicas) && replicas[i] != "" {
		name = replicas[i]
	}
	all := append([]any{name}, args...)
	return fmt.Errorf("gc: %s: "+format, all...)
}
