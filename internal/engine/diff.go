package engine

import (
	"fmt"
	"path/filepath"
	"sort"

	"cdag/internal/fingerprint"
)

// diffFingerprint compares a previous fingerprint with the current one and
// returns human-readable changes. It is the source of the "why rebuilt"
// explanations; only inputs that actually changed are reported.
func diffFingerprint(prev, cur fingerprint.Fingerprint) []Change {
	var changes []Change

	// Input files: added, removed, content changed.
	prevIn := indexRefs(prev.Inputs)
	curIn := indexRefs(cur.Inputs)
	for _, p := range sortedKeys(curIn) {
		old, ok := prevIn[p]
		switch {
		case !ok:
			changes = append(changes, Change{Type: "input_added",
				Detail: fmt.Sprintf("input file %q is newly declared", p)})
		case old.SHA != curIn[p].SHA:
			changes = append(changes, Change{Type: "input_changed",
				Detail: fmt.Sprintf("input file %q content changed (%s -> %s)", p, old.SHA[:10], curIn[p].SHA[:10])})
		case old.Mode != curIn[p].Mode:
			changes = append(changes, Change{Type: "input_mode_changed",
				Detail: fmt.Sprintf("input file %q permissions changed (%04o -> %04o)", p, old.Mode, curIn[p].Mode)})
		}
	}
	for _, p := range sortedKeys(prevIn) {
		if _, ok := curIn[p]; !ok {
			changes = append(changes, Change{Type: "input_removed",
				Detail: fmt.Sprintf("input file %q is no longer declared", p)})
		}
	}

	// Dependencies: added, removed, dep fingerprint changed, output content changed.
	prevDeps := indexDeps(prev.Deps)
	curDeps := indexDeps(cur.Deps)
	for _, id := range sortedDepKeys(curDeps) {
		old, ok := prevDeps[id]
		switch {
		case !ok:
			changes = append(changes, Change{Type: "dependency_added",
				Detail: fmt.Sprintf("dependency %q is newly declared", id)})
		case old.Fingerprint != curDeps[id].Fingerprint:
			changes = append(changes, Change{Type: "dependency_changed",
				Detail: fmt.Sprintf("transitive dependency %q changed (%s -> %s)", id,
					shortKey(old.Fingerprint), shortKey(curDeps[id].Fingerprint))})
		default:
			// Fingerprint equal: outputs must be equal transitively, but
			// double-check output hashes for precise messages.
			for _, msg := range diffOutputs(fmt.Sprintf("dependency %q output", id), old.Outputs, curDeps[id].Outputs) {
				changes = append(changes, msg)
			}
		}
	}
	for _, id := range sortedDepKeys(prevDeps) {
		if _, ok := curDeps[id]; !ok {
			changes = append(changes, Change{Type: "dependency_removed",
				Detail: fmt.Sprintf("dependency %q was removed", id)})
		}
	}

	// Params.
	prevParams := indexKV(prev.Params)
	curParams := indexKV(cur.Params)
	for _, k := range sortedStringKeys(curParams) {
		old, ok := prevParams[k]
		switch {
		case !ok:
			changes = append(changes, Change{Type: "param_added",
				Detail: fmt.Sprintf("parameter %q newly set to %q", k, curParams[k])})
		case old != curParams[k]:
			changes = append(changes, Change{Type: "param_changed",
				Detail: fmt.Sprintf("parameter %q changed: %q -> %q", k, old, curParams[k])})
		}
	}
	for _, k := range sortedStringKeys(prevParams) {
		if _, ok := curParams[k]; !ok {
			changes = append(changes, Change{Type: "param_removed",
				Detail: fmt.Sprintf("parameter %q removed (was %q)", k, prevParams[k])})
		}
	}

	// Declared environment variables.
	prevEnv := indexKV(prev.Env)
	curEnv := indexKV(cur.Env)
	for _, k := range sortedStringKeys(curEnv) {
		old, ok := prevEnv[k]
		switch {
		case !ok:
			changes = append(changes, Change{Type: "env_added",
				Detail: fmt.Sprintf("environment variable %q newly declared (value %q)", k, curEnv[k])})
		case old != curEnv[k]:
			changes = append(changes, Change{Type: "env_changed",
				Detail: fmt.Sprintf("declared environment variable %q changed: %q -> %q", k, old, curEnv[k])})
		}
	}
	for _, k := range sortedStringKeys(prevEnv) {
		if _, ok := curEnv[k]; !ok {
			changes = append(changes, Change{Type: "env_removed",
				Detail: fmt.Sprintf("environment variable %q no longer declared (was %q)", k, prevEnv[k])})
		}
	}

	// Tool versions.
	prevTools := indexTools(prev.Tools)
	curTools := indexTools(cur.Tools)
	for _, k := range sortedStringKeys(curTools) {
		old, ok := prevTools[k]
		switch {
		case !ok:
			changes = append(changes, Change{Type: "tool_added",
				Detail: fmt.Sprintf("tool %q newly declared with version %q", k, curTools[k])})
		case old != curTools[k]:
			changes = append(changes, Change{Type: "tool_version_changed",
				Detail: fmt.Sprintf("tool %q version changed: %q -> %q", k, old, curTools[k])})
		}
	}
	for _, k := range sortedStringKeys(prevTools) {
		if _, ok := curTools[k]; !ok {
			changes = append(changes, Change{Type: "tool_removed",
				Detail: fmt.Sprintf("tool %q no longer declared (was %q)", k, prevTools[k])})
		}
	}

	return changes
}

func diffOutputs(prefix string, old, cur []fingerprint.FileRef) []Change {
	var out []Change
	om := indexRefs(old)
	cm := indexRefs(cur)
	for p := range cm {
		oref, ok := om[p]
		switch {
		case !ok:
			out = append(out, Change{Type: "output_added",
				Detail: fmt.Sprintf("%s %q newly produced", prefix, p)})
		case oref.SHA != cm[p].SHA:
			out = append(out, Change{Type: "output_changed",
				Detail: fmt.Sprintf("%s %q content changed (%s -> %s)", prefix, p, oref.SHA[:10], cm[p].SHA[:10])})
		}
	}
	for p := range om {
		if _, ok := cm[p]; !ok {
			out = append(out, Change{Type: "output_removed",
				Detail: fmt.Sprintf("%s %q no longer produced", prefix, p)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Detail < out[j].Detail })
	return out
}

func indexRefs(rs []fingerprint.FileRef) map[string]fingerprint.FileRef {
	m := map[string]fingerprint.FileRef{}
	for _, r := range rs {
		m[r.Path] = r
	}
	return m
}

func indexDeps(ds []fingerprint.DepRef) map[string]fingerprint.DepRef {
	m := map[string]fingerprint.DepRef{}
	for _, d := range ds {
		m[d.ID] = d
	}
	return m
}

func indexKV(kvs []fingerprint.KV) map[string]string {
	m := map[string]string{}
	for _, kv := range kvs {
		m[kv.Key] = kv.Val
	}
	return m
}

func indexTools(ts []fingerprint.ToolVersion) map[string]string {
	m := map[string]string{}
	for _, t := range ts {
		m[t.Name] = t.Version
	}
	return m
}

func sortedKeys(m map[string]fingerprint.FileRef) []string {
	return mapKeys(m)
}

func sortedDepKeys(m map[string]fingerprint.DepRef) []string {
	return mapKeys(m)
}

func sortedStringKeys(m map[string]string) []string {
	return mapKeys(m)
}

func mapKeys[V any](m map[string]V) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

func filepathAbs(p string) (string, error) {
	abs := p
	if !filepath.IsAbs(abs) {
		a, err := filepath.Abs(abs)
		if err != nil {
			return "", err
		}
		abs = a
	}
	return filepath.Clean(abs), nil
}
