// Package service orchestrates full and incremental dependency scans.
//
// The service is purely local: it reads files under a user-provided source
// root, never executes external commands, and never talks to any network
// service. Scan results are cached under a cache directory that must be
// separate from the source root.
package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"depscan/internal/graph"
	"depscan/internal/scanner"
)

// Target is a named build unit: a set of source files.
type Target struct {
	Name    string   `json:"name"`
	Sources []string `json:"sources"` // root-relative slash paths
}

// Config describes one scan request.
type Config struct {
	SourceRoot  string   `json:"sourceRoot"`            // directory to scan
	IncludeDirs []string `json:"includeDirs,omitempty"` // extra include search dirs (relative to root or absolute)
	Targets     []Target `json:"targets,omitempty"`     // build targets for affected-target reporting
}

// Unresolved is an include that matched no file on disk.
type Unresolved struct {
	File    string `json:"file"`
	Include string `json:"include"`
	Angle   bool   `json:"angle"`
	Line    int    `json:"line"`
}

// Result is the stable output of a scan.
type Result struct {
	Graph           map[string][]string `json:"graph"` // file -> sorted direct dependencies
	Cycles          [][]string          `json:"cycles"`
	Unresolved      []Unresolved        `json:"unresolved"`
	AffectedFiles   []string            `json:"affectedFiles"`
	AffectedTargets []string            `json:"affectedTargets"`
	FilesScanned    int                 `json:"filesScanned"`
	CachePath       string              `json:"cachePath"`
}

// sourceExts are the file extensions treated as C-family sources.
var sourceExts = map[string]bool{
	".c": true, ".h": true,
	".cc": true, ".hh": true,
	".cpp": true, ".hpp": true,
	".cxx": true, ".hxx": true,
}

// Service performs scans and persists results in a cache directory that
// must be separate from any scanned source root.
type Service struct {
	CacheDir string
}

// NewService validates that the cache directory is usable.
func NewService(cacheDir string) (*Service, error) {
	if cacheDir == "" {
		return nil, errors.New("cache directory must not be empty")
	}
	abs, err := filepath.Abs(cacheDir)
	if err != nil {
		return nil, fmt.Errorf("cache directory: %w", err)
	}
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return nil, fmt.Errorf("cache directory: %w", err)
	}
	return &Service{CacheDir: abs}, nil
}

// cacheFile returns the cache file path for a source root.
func (s *Service) cacheFile(rootAbs string) string {
	sum := sha256.Sum256([]byte(rootAbs))
	return filepath.Join(s.CacheDir, "scan-"+hex.EncodeToString(sum[:16])+".json")
}

// ErrNoCache marks incremental scans requested before any full scan.
var ErrNoCache = errors.New("no cached scan for this source root; run a full scan first")

// ConfigError marks request-level configuration problems (HTTP 400).
type ConfigError struct{ msg string }

func (e *ConfigError) Error() string { return e.msg }

// checkSeparation enforces that the cache directory is neither inside the
// source root nor equal to it (and vice versa), keeping cache and working
// tree strictly separate.
func (s *Service) checkSeparation(rootAbs string) error {
	rel, err := filepath.Rel(rootAbs, s.CacheDir)
	if err != nil {
		return &ConfigError{fmt.Sprintf("cannot relate cache dir to source root: %v", err)}
	}
	if rel == "." || (!strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != "..") {
		return &ConfigError{fmt.Sprintf("cache directory %q must be separate from source root %q", s.CacheDir, rootAbs)}
	}
	rel2, err := filepath.Rel(s.CacheDir, rootAbs)
	if err == nil && rel2 == "." {
		return &ConfigError{fmt.Sprintf("cache directory %q must be separate from source root %q", s.CacheDir, rootAbs)}
	}
	return nil
}

// normalize validates cfg and returns the absolute root plus absolute
// include dirs.
func (s *Service) normalize(cfg Config) (rootAbs string, incDirs []string, err error) {
	if cfg.SourceRoot == "" {
		return "", nil, &ConfigError{"sourceRoot is required"}
	}
	rootAbs, err = filepath.Abs(cfg.SourceRoot)
	if err != nil {
		return "", nil, &ConfigError{fmt.Sprintf("sourceRoot: %v", err)}
	}
	info, err := os.Stat(rootAbs)
	if err != nil || !info.IsDir() {
		return "", nil, &ConfigError{fmt.Sprintf("sourceRoot %q is not a directory", cfg.SourceRoot)}
	}
	if err := s.checkSeparation(rootAbs); err != nil {
		return "", nil, err
	}
	for _, d := range cfg.IncludeDirs {
		if !filepath.IsAbs(d) {
			d = filepath.Join(rootAbs, d)
		}
		incDirs = append(incDirs, filepath.Clean(d))
	}
	return rootAbs, incDirs, nil
}

// scanFile scans one root-relative file and returns its resolved deps and
// unresolved includes.
func scanOne(rootAbs string, incDirs []string, rel string) (deps []string, unres []Unresolved, err error) {
	data, err := os.ReadFile(filepath.Join(rootAbs, filepath.FromSlash(rel)))
	if err != nil {
		return nil, nil, err
	}
	incs, err := scanner.Scan(string(data))
	if err != nil {
		return nil, nil, fmt.Errorf("%s: %w", rel, err)
	}
	res := &scanner.Resolver{Root: rootAbs, IncludeDirs: incDirs}
	for _, inc := range incs {
		if to, ok := res.Resolve(inc, rel); ok {
			deps = append(deps, to)
		} else {
			unres = append(unres, Unresolved{File: rel, Include: inc.Path, Angle: inc.Angle, Line: inc.Line})
		}
	}
	return deps, unres, nil
}

// listSources returns all source files under rootAbs, root-relative and sorted.
func listSources(rootAbs string) ([]string, error) {
	var out []string
	err := filepath.WalkDir(rootAbs, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if strings.HasPrefix(d.Name(), ".") && path != rootAbs {
				return filepath.SkipDir // skip hidden dirs (.git etc.)
			}
			return nil
		}
		if sourceExts[strings.ToLower(filepath.Ext(d.Name()))] {
			rel, err := filepath.Rel(rootAbs, path)
			if err != nil {
				return err
			}
			out = append(out, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(out)
	return out, nil
}

// cacheState is what gets persisted between scans.
type cacheState struct {
	Config Config              `json:"config"`
	Graph  map[string][]string `json:"graph"`
}

func (s *Service) saveCache(rootAbs string, cfg Config, g *graph.Graph) (string, error) {
	path := s.cacheFile(rootAbs)
	state := cacheState{Config: cfg, Graph: g.Deps}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return "", fmt.Errorf("write cache: %w", err)
	}
	return path, nil
}

// LoadCache returns the cached graph for a source root, or nil if none.
func (s *Service) LoadCache(root string) (*graph.Graph, error) {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(s.cacheFile(rootAbs))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var state cacheState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("read cache: %w", err)
	}
	return &graph.Graph{Deps: state.Graph}, nil
}

// buildResult assembles the stable scan output. affected is the
// precomputed set of files impacted by the scan (reverse closure).
func (s *Service) buildResult(g *graph.Graph, unres []Unresolved, affected []string, cfg Config, filesScanned int, cachePath string) *Result {
	affectedSet := map[string]bool{}
	for _, f := range affected {
		affectedSet[f] = true
	}
	var affectedTargets []string
	for _, t := range cfg.Targets {
		for _, src := range t.Sources {
			if affectedSet[src] {
				affectedTargets = append(affectedTargets, t.Name)
				break
			}
		}
	}
	sort.Strings(affectedTargets)
	if unres == nil {
		unres = []Unresolved{}
	}
	sort.Slice(unres, func(i, j int) bool {
		if unres[i].File != unres[j].File {
			return unres[i].File < unres[j].File
		}
		return unres[i].Line < unres[j].Line
	})
	cycles := g.Cycles()
	if cycles == nil {
		cycles = [][]string{}
	}
	return &Result{
		Graph:           g.Deps,
		Cycles:          cycles,
		Unresolved:      unres,
		AffectedFiles:   affected,
		AffectedTargets: affectedTargets,
		FilesScanned:    filesScanned,
		CachePath:       cachePath,
	}
}

// FullScan scans every source file under cfg.SourceRoot, rebuilds the
// dependency graph from scratch, and updates the cache.
func (s *Service) FullScan(cfg Config) (*Result, error) {
	rootAbs, incDirs, err := s.normalize(cfg)
	if err != nil {
		return nil, err
	}
	files, err := listSources(rootAbs)
	if err != nil {
		return nil, fmt.Errorf("list sources: %w", err)
	}
	g := graph.New()
	var unres []Unresolved
	for _, rel := range files {
		deps, u, err := scanOne(rootAbs, incDirs, rel)
		if err != nil {
			return nil, err
		}
		g.Set(rel, deps)
		unres = append(unres, u...)
	}
	cachePath, err := s.saveCache(rootAbs, cfg, g)
	if err != nil {
		return nil, err
	}
	// A full scan invalidates everything: all files are "affected".
	return s.buildResult(g, unres, g.Files(), cfg, len(files), cachePath), nil
}

// Incremental updates the cached graph for the files the caller explicitly
// reports as changed or deleted, then recomputes affected files/targets.
// Changed files that no longer exist are treated as deleted.
func (s *Service) Incremental(cfg Config, changed, deleted []string) (*Result, error) {
	rootAbs, incDirs, err := s.normalize(cfg)
	if err != nil {
		return nil, err
	}
	g, err := s.LoadCache(rootAbs)
	if err != nil {
		return nil, err
	}
	if g == nil {
		return nil, ErrNoCache
	}

	// Compute the reverse closure against the OLD graph first: once a
	// deleted node is removed, its inbound edges vanish and its former
	// dependents would no longer be found.
	affected := g.Affected(append(append([]string(nil), changed...), deleted...))

	g.Remove(deleted...)
	var unres []Unresolved
	scanned := 0
	for _, rel := range changed {
		rel = filepath.ToSlash(filepath.Clean(filepath.FromSlash(rel)))
		if _, err := os.Stat(filepath.Join(rootAbs, filepath.FromSlash(rel))); errors.Is(err, os.ErrNotExist) {
			g.Remove(rel) // changed file vanished: treat as deleted
			continue
		}
		deps, u, err := scanOne(rootAbs, incDirs, rel)
		if err != nil {
			return nil, err
		}
		g.Set(rel, deps)
		unres = append(unres, u...)
		scanned++
	}
	cachePath, err := s.saveCache(rootAbs, cfg, g)
	if err != nil {
		return nil, err
	}
	return s.buildResult(g, unres, affected, cfg, scanned, cachePath), nil
}
