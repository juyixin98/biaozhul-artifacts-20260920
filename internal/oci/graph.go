package oci

import "fmt"

// DetectCycle reports whether the directed graph described by adj contains a
// reachable cycle, returning an error naming the offending node. It is used
// as defense-in-depth after walking an index graph: with content-addressed
// blobs a real cycle cannot be constructed, but a corrupt or hand-built
// metadata graph must never send traversal into an infinite loop.
func DetectCycle(adj map[string][]string) error {
	const (
		white = 0 // unvisited
		gray  = 1 // on current path
		black = 2 // fully explored
	)
	color := map[string]int{}
	var visit func(n string) error
	visit = func(n string) error {
		switch color[n] {
		case gray:
			return fmt.Errorf("circular reference detected at %s", n)
		case black:
			return nil
		}
		color[n] = gray
		for _, m := range adj[n] {
			if err := visit(m); err != nil {
				return err
			}
		}
		color[n] = black
		return nil
	}
	for n := range adj {
		if color[n] == white {
			if err := visit(n); err != nil {
				return err
			}
		}
	}
	return nil
}
