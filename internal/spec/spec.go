// Package spec defines the declarative build specification: a project is an
// ordered DAG of build actions. Each action names one tool, the sources it
// reads, the upstream artifacts it consumes and the outputs it produces.
package spec

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"

	"bis/internal/safeio"
)

// Action is one declarative build step.
type Action struct {
	ID       string            `json:"id"`
	Tool     string            `json:"tool"`
	Args     []string          `json:"args,omitempty"`
	Sources  []string          `json:"sources,omitempty"`
	Upstream map[string]string `json:"upstream,omitempty"` // local name -> upstream action ID
	Outputs  []string          `json:"outputs"`
}

// Project is the contents of a project's build.json.
type Project struct {
	Name    string   `json:"name"`
	Actions []Action `json:"actions"`
}

var validName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// Load reads and validates build.json from the given project directory.
func Load(projectDir string) (*Project, error) {
	p, err := safeio.ResolveWithin(projectDir, "build.json")
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	var pr Project
	if err := json.Unmarshal(b, &pr); err != nil {
		return nil, fmt.Errorf("build.json: %w", err)
	}
	if err := pr.Validate(); err != nil {
		return nil, err
	}
	return &pr, nil
}

// ErrCycle means the action graph contains a dependency cycle.
var ErrCycle = errors.New("cyclic action graph")

// Validate performs all static checks that do not require the filesystem:
// names, references, output declarations and DAG structure.
func (p *Project) Validate() error {
	if !validName.MatchString(p.Name) {
		return fmt.Errorf("invalid project name %q", p.Name)
	}
	if len(p.Actions) == 0 {
		return errors.New("project has no actions")
	}
	seen := make(map[string]bool, len(p.Actions))
	outputs := make(map[string]string) // output path -> producing action
	for i := range p.Actions {
		a := &p.Actions[i]
		if !validName.MatchString(a.ID) {
			return fmt.Errorf("invalid action id %q", a.ID)
		}
		if seen[a.ID] {
			return fmt.Errorf("duplicate action id %q", a.ID)
		}
		seen[a.ID] = true
		if a.Tool == "" {
			return fmt.Errorf("action %q: tool is required", a.ID)
		}
		if len(a.Outputs) == 0 {
			return fmt.Errorf("action %q: at least one output is required", a.ID)
		}
		for _, o := range a.Outputs {
			if err := checkRel(o); err != nil {
				return fmt.Errorf("action %q: output %q: %w", a.ID, o, err)
			}
			if isReservedSegment(o) {
				return fmt.Errorf("action %q: output %q uses reserved prefix %q", a.ID, o, reservedDir)
			}
			if other, dup := outputs[o]; dup {
				return fmt.Errorf("output %q declared by both %q and %q", o, other, a.ID)
			}
			outputs[o] = a.ID
		}
		for _, s := range a.Sources {
			if err := checkRel(s); err != nil {
				return fmt.Errorf("action %q: source %q: %w", a.ID, s, err)
			}
			if isReservedSegment(s) {
				return fmt.Errorf("action %q: source %q uses reserved prefix %q", a.ID, s, reservedDir)
			}
		}
		for local, up := range a.Upstream {
			if !validName.MatchString(local) {
				return fmt.Errorf("action %q: invalid upstream local name %q", a.ID, local)
			}
			if local == "src" {
				return fmt.Errorf("action %q: upstream local name %q is reserved", a.ID, local)
			}
			_ = up // existence checked in the reference pass below
		}
	}
	// Upstream references must point at declared actions, and an action may
	// not consume itself directly.
	for i := range p.Actions {
		a := &p.Actions[i]
		for local, up := range a.Upstream {
			if !seen[up] {
				return fmt.Errorf("action %q: upstream %q references unknown action %q", a.ID, local, up)
			}
			if up == a.ID {
				return fmt.Errorf("action %q depends on itself", a.ID)
			}
		}
	}
	if _, err := p.TopoOrder(); err != nil {
		return err
	}
	return nil
}

// TopoOrder returns action indices in dependency order (Kahn's algorithm).
// Every upstream precedes its dependents. Returns ErrCycle on a cycle.
func (p *Project) TopoOrder() ([]int, error) {
	n := len(p.Actions)
	idx := make(map[string]int, n)
	indeg := make([]int, n)
	adj := make([][]int, n)
	for i := range p.Actions {
		idx[p.Actions[i].ID] = i
	}
	for i := range p.Actions {
		a := &p.Actions[i]
		for _, up := range a.Upstream {
			j := idx[up]
			adj[j] = append(adj[j], i)
			indeg[i]++
		}
	}
	queue := make([]int, 0, n)
	for i, d := range indeg {
		if d == 0 {
			queue = append(queue, i)
		}
	}
	order := make([]int, 0, n)
	for len(queue) > 0 {
		i := queue[0]
		queue = queue[1:]
		order = append(order, i)
		for _, j := range adj[i] {
			indeg[j]--
			if indeg[j] == 0 {
				queue = append(queue, j)
			}
		}
	}
	if len(order) != n {
		return nil, fmt.Errorf("%w: %d action(s) participate in a cycle", ErrCycle, n-len(order))
	}
	return order, nil
}

// Action looks up an action by ID.
func (p *Project) Action(id string) *Action {
	for i := range p.Actions {
		if p.Actions[i].ID == id {
			return &p.Actions[i]
		}
	}
	return nil
}

func checkRel(p string) error {
	if p == "" {
		return errors.New("empty path")
	}
	cleaned, err := safeio.ResolveWithinSafeLexical(p)
	if err != nil {
		return err
	}
	if cleaned != p {
		return fmt.Errorf("path must be clean and relative (use %q)", cleaned)
	}
	return nil
}

// reservedDir is the work-directory subtree where upstream artifacts are
// materialized. Declared sources and outputs may not use it.
const reservedDir = "in"

func isReservedSegment(p string) bool {
	return p == reservedDir || strings.HasPrefix(p, reservedDir+"/")
}
