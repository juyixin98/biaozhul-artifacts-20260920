// Package spec defines the on-disk project specification and its loader.
// The same format is used by the CLI (project JSON file) and the HTTP API
// (POST /v1/projects request body).
package spec

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"cdag/internal/graph"
)

// Project is a named build graph rooted at a workspace directory.
type Project struct {
	// ID is the unique project identifier.
	ID string `json:"id"`
	// Workdir is the workspace root directory. Relative paths are
	// resolved against the process working directory; absolute paths
	// are honored as given.
	Workdir string `json:"workdir"`
	// Graph holds the nodes.
	Graph graph.Graph `json:"graph"`
}

// LoadFile reads and validates a project specification from path.
// Relative Workdir is resolved against the directory containing the file.
func LoadFile(path string) (*Project, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read spec: %w", err)
	}
	var p Project
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, fmt.Errorf("parse spec: %w", err)
	}
	base := filepath.Dir(path)
	if !filepath.IsAbs(p.Workdir) {
		p.Workdir = filepath.Join(base, p.Workdir)
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return &p, nil
}

// Validate checks the project envelope and delegates to graph validation.
func (p *Project) Validate() error {
	if p.ID == "" {
		return fmt.Errorf("project id is empty")
	}
	if p.Workdir == "" {
		return fmt.Errorf("project %q: workdir is empty", p.ID)
	}
	if len(p.Graph.Nodes) == 0 {
		return fmt.Errorf("project %q: graph has no nodes", p.ID)
	}
	if ge := p.Graph.Validate(); ge != nil {
		return fmt.Errorf("project %q: %w", p.ID, ge)
	}
	return nil
}
