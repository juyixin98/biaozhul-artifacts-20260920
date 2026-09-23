// Package graph 定义内容驱动构建图的数据结构、校验与 DAG 操作。
package graph

import (
	"fmt"
	"path"
	"sort"
	"strings"
	"text/template"
	"text/template/parse"
)

// Tool 描述一个构建工具。ToolVersion 是工具自身版本的声明，
// 改变版本会使所有使用该工具的节点缓存失效。
type Tool struct {
	Name        string `json:"name"`
	ToolVersion string `json:"tool_version"`
	// Command 为可执行程序名；Shell 为 true 时通过 `sh -c` 执行整行命令。
	Command []string `json:"command"`
	Shell   bool     `json:"shell,omitempty"`
	// DeclaredEnv 声明该工具允许从父进程继承的环境变量白名单；其余变量不透传。
	DeclaredEnv []string `json:"declared_env,omitempty"`
	// Env 为节点级固定环境变量（Key=Value），始终写入缓存键。
	Env map[string]string `json:"env,omitempty"`
}

// Node 描述构建图中的一个节点。
type Node struct {
	Name       string            `json:"name"`
	Tool       string            `json:"tool"`
	Inputs     []string          `json:"inputs,omitempty"` // 相对工作目录的文件/目录
	Deps       []string          `json:"deps,omitempty"`   // 依赖的其它节点
	Outputs    []string          `json:"outputs,omitempty"`
	Params     map[string]string `json:"params,omitempty"`
	Args       []string          `json:"args,omitempty"` // 命令参数；{{.KEY}} 引用 Params
	TimeoutSec int               `json:"timeout_sec,omitempty"`
}

// Spec 是一份构建图规格。
type Spec struct {
	Version string           `json:"version"`
	Tools   map[string]*Tool `json:"tools"`
	Nodes   []*Node          `json:"nodes"`
}

// Validate 校验规格，返回所有可静态发现的问题。
func (s *Spec) Validate() error {
	if s.Version == "" {
		s.Version = "1"
	}
	if len(s.Tools) == 0 {
		return fmt.Errorf("spec requires at least one tool")
	}
	if len(s.Nodes) == 0 {
		return fmt.Errorf("spec requires at least one node")
	}
	for name, t := range s.Tools {
		if name == "" {
			return fmt.Errorf("tool with empty name")
		}
		if t.Name == "" {
			t.Name = name
		}
		if len(t.Command) == 0 || t.Command[0] == "" {
			return fmt.Errorf("tool %q requires a non-empty command", name)
		}
	}
	seen := map[string]bool{}
	for _, n := range s.Nodes {
		if n.Name == "" {
			return fmt.Errorf("node with empty name")
		}
		if seen[n.Name] {
			return fmt.Errorf("duplicate node name %q", n.Name)
		}
		seen[n.Name] = true
	}
	for _, n := range s.Nodes {
		if _, ok := s.Tools[n.Tool]; !ok {
			return fmt.Errorf("node %q references unknown tool %q", n.Name, n.Tool)
		}
		for _, d := range n.Deps {
			if !seen[d] {
				return fmt.Errorf("node %q depends on unknown node %q", n.Name, d)
			}
			if d == n.Name {
				return fmt.Errorf("node %q depends on itself", n.Name)
			}
		}
		for _, p := range append(append([]string{}, n.Inputs...), n.Outputs...) {
			if err := checkRelPath(p); err != nil {
				return fmt.Errorf("node %q: %w", n.Name, err)
			}
		}
		// 预解析参数模板，保证模板语法与引用键合法。
		for i, a := range n.Args {
			tpl, err := template.New(n.Name).Parse(a)
			if err != nil {
				return fmt.Errorf("node %q arg %d has invalid template: %w", n.Name, i, err)
			}
			if err := validateTemplateKeys(tpl, n.Params); err != nil {
				return fmt.Errorf("node %q arg %d: %w", n.Name, i, err)
			}
		}
	}
	return nil
}

// NodeByName 返回指定节点。
func (s *Spec) NodeByName(name string) *Node {
	for _, n := range s.Nodes {
		if n.Name == name {
			return n
		}
	}
	return nil
}

func checkRelPath(p string) error {
	if p == "" {
		return fmt.Errorf("empty path")
	}
	if path.IsAbs(p) {
		return fmt.Errorf("path %q must be relative to the work directory", p)
	}
	clean := path.Clean(p)
	if clean == ".." || strings.HasPrefix(clean, "../") {
		return fmt.Errorf("path %q escapes the work directory", p)
	}
	if strings.Contains(p, "\x00") {
		return fmt.Errorf("path %q contains NUL byte", p)
	}
	return nil
}

// Reachable 返回从 targets 出发（沿 Deps）可达的全部节点名，包含 targets 自身。
// 空 targets 表示全部节点。
func (s *Spec) Reachable(targets []string) (map[string]bool, error) {
	reach := map[string]bool{}
	stack := append([]string{}, targets...)
	if len(targets) == 0 {
		for _, n := range s.Nodes {
			stack = append(stack, n.Name)
		}
	}
	for len(stack) > 0 {
		name := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if reach[name] {
			continue
		}
		n := s.NodeByName(name)
		if n == nil {
			return nil, fmt.Errorf("unknown target node %q", name)
		}
		reach[name] = true
		stack = append(stack, n.Deps...)
	}
	return reach, nil
}

// CycleError 描述一个依赖环。
type CycleError struct {
	Cycle []string `json:"cycle"`
}

func (e *CycleError) Error() string {
	return fmt.Sprintf("dependency cycle detected: %s", strings.Join(e.Cycle, " -> "))
}

// TopoOrder 对 included 中的节点按依赖关系做确定性拓扑排序。
// 若存在环，返回 *CycleError，Cycle 为环上的节点序列（首尾相同）。
func (s *Spec) TopoOrder(included map[string]bool) ([]string, error) {
	const (
		white = 0 // 未访问
		gray  = 1 // 在当前 DFS 栈中
		black = 2 // 已完成
	)
	color := map[string]int{}
	var stack []frame
	var order []string

	roots := make([]string, 0, len(included))
	for name := range included {
		roots = append(roots, name)
	}
	sort.Strings(roots)

	// 迭代式 DFS，维护显式栈以提取环路径。
	for _, root := range roots {
		if color[root] != white {
			continue
		}
		stack = append(stack, frame{name: root, idx: 0})
		color[root] = gray
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			n := s.NodeByName(top.name)
			deps := sortedDepsIn(n, included)
			if top.idx < len(deps) {
				next := deps[top.idx]
				top.idx++
				switch color[next] {
				case white:
					color[next] = gray
					stack = append(stack, frame{name: next, idx: 0})
				case gray:
					// 从栈中找到 next，构成环。
					cyc := []string{}
					for _, f := range stack {
						if f.name == next {
							cyc = append(cyc, f.name)
						} else if len(cyc) > 0 {
							cyc = append(cyc, f.name)
						}
					}
					cyc = append(cyc, next)
					return nil, &CycleError{Cycle: cyc}
				}
				continue
			}
			color[top.name] = black
			order = append(order, top.name)
			stack = stack[:len(stack)-1]
		}
	}
	return order, nil
}

type frame struct {
	name string
	idx  int
}

func sortedDepsIn(n *Node, included map[string]bool) []string {
	deps := make([]string, 0, len(n.Deps))
	for _, d := range n.Deps {
		if included[d] {
			deps = append(deps, d)
		}
	}
	sort.Strings(deps)
	return deps
}

// templateKeys 遍历模板 AST 收集 {{.KEY}} 引用，确保只引用已声明的 Params。
// 不能靠执行渲染检查：访问缺失的 map 键只会得到 <no value> 而不报错。
// text/template/parse 没有导出 Walk，这里手写一遍节点遍历。
func validateTemplateKeys(tpl *template.Template, params map[string]string) error {
	bad := map[string]bool{}
	walk := func(n parse.Node) { walkTemplateNode(n, params, bad) }
	for _, tree := range tpl.Templates() {
		if tree.Root != nil {
			walk(tree.Root)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	keys := make([]string, 0, len(bad))
	for k := range bad {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return fmt.Errorf("template references undefined param(s): %s", strings.Join(keys, ", "))
}

func walkTemplateNode(n parse.Node, params map[string]string, bad map[string]bool) {
	switch t := n.(type) {
	case *parse.ListNode:
		if t == nil {
			return
		}
		for _, c := range t.Nodes {
			walkTemplateNode(c, params, bad)
		}
	case *parse.TextNode:
		// no fields
	case *parse.FieldNode:
		if len(t.Ident) > 0 && t.Ident[0] != "" {
			if _, ok := params[t.Ident[0]]; !ok {
				bad[t.Ident[0]] = true
			}
		}
	case *parse.ChainNode:
		if len(t.Field) > 0 {
			if _, ok := params[t.Field[0]]; !ok {
				bad[t.Field[0]] = true
			}
		}
		if t.Node != nil {
			walkTemplateNode(t.Node, params, bad)
		}
	case *parse.PipeNode:
		for _, c := range t.Cmds {
			walkTemplateNode(c, params, bad)
		}
		if t.Decl != nil {
			for _, v := range t.Decl {
				walkTemplateNode(v, params, bad)
			}
		}
	case *parse.CommandNode:
		for _, c := range t.Args {
			walkTemplateNode(c, params, bad)
		}
	case *parse.ActionNode:
		if t.Pipe != nil {
			walkTemplateNode(t.Pipe, params, bad)
		}
	case *parse.IfNode:
		if t.Pipe != nil {
			walkTemplateNode(t.Pipe, params, bad)
		}
		if t.List != nil {
			walkTemplateNode(t.List, params, bad)
		}
		if t.ElseList != nil {
			walkTemplateNode(t.ElseList, params, bad)
		}
	case *parse.RangeNode:
		if t.Pipe != nil {
			walkTemplateNode(t.Pipe, params, bad)
		}
		if t.List != nil {
			walkTemplateNode(t.List, params, bad)
		}
		if t.ElseList != nil {
			walkTemplateNode(t.ElseList, params, bad)
		}
	case *parse.WithNode:
		if t.Pipe != nil {
			walkTemplateNode(t.Pipe, params, bad)
		}
		if t.List != nil {
			walkTemplateNode(t.List, params, bad)
		}
		if t.ElseList != nil {
			walkTemplateNode(t.ElseList, params, bad)
		}
	case *parse.TemplateNode:
		if t.Pipe != nil {
			walkTemplateNode(t.Pipe, params, bad)
		}
	case *parse.VariableNode, *parse.IdentifierNode, *parse.NumberNode,
		*parse.BoolNode, *parse.StringNode, *parse.NilNode, *parse.DotNode,
		*parse.CommentNode, *parse.BreakNode, *parse.ContinueNode:
		// 叶子或无需校验的节点
	}
}
