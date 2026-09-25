package scanner

import (
	"os"
	"path/filepath"
)

// Resolver maps include operands to files on disk.
type Resolver struct {
	// Root is the absolute source root. Resolved paths are reported
	// relative to it, slash-separated, for stable output.
	Root string
	// IncludeDirs are absolute directories searched for angled includes,
	// and for quoted includes after the including file's own directory.
	IncludeDirs []string
}

// Resolve returns the root-relative slash path of the file an include
// refers to, and whether it was found.
//
// Quoted includes search the directory of the including file first, then
// IncludeDirs in order. Angled includes search IncludeDirs only. The first
// match wins, which is what disambiguates same-name headers.
func (r *Resolver) Resolve(inc Include, fromRel string) (string, bool) {
	var candidates []string
	if !inc.Angle {
		candidates = append(candidates,
			filepath.Join(r.Root, filepath.Dir(filepath.FromSlash(fromRel)), filepath.FromSlash(inc.Path)))
	}
	for _, d := range r.IncludeDirs {
		candidates = append(candidates, filepath.Join(d, filepath.FromSlash(inc.Path)))
	}
	for _, c := range candidates {
		info, err := os.Stat(c)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		rel, err := filepath.Rel(r.Root, c)
		if err != nil {
			continue
		}
		return filepath.ToSlash(rel), true
	}
	return "", false
}
