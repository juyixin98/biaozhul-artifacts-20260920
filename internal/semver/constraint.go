package semver

import (
	"errors"
	"fmt"
	"strings"
)

// ErrInvalidConstraint 在约束串不符合本包支持的简化语法时返回。
var ErrInvalidConstraint = errors.New("invalid version constraint")

// Op 枚举单个原子比较的运算符。
type Op string

const (
	OpEq  Op = "="
	OpNeq Op = "!="
	OpGt  Op = ">"
	OpGte Op = ">="
	OpLt  Op = "<"
	OpLte Op = "<="
)

// Comparator 是一个原子版本比较，例如 >=1.2.3。
type Comparator struct {
	Op Op
	V  Version
}

// cgroup 是一个以空格/逗号连接的“与”组，gate 为该组显式提及的预发布基元。
type cgroup struct {
	comps []Comparator
	gate  map[[3]int64]struct{}
}

// Constraint 是解析后的区间约束，多个组之间为“或”（||），
// 组内比较之间为“与”。零个比较的组匹配任意版本。
type Constraint struct {
	Raw    string
	groups []cgroup
}

// MustParse 是 ParseConstraint 的便捷封装，失败时 panic（仅用于测试常量）。
func MustParse(s string) Constraint {
	c, err := ParseConstraint(s)
	if err != nil {
		panic(err)
	}
	return c
}

// ParseConstraint 解析约束串，语法见 README“约束语法”一节：
//
//	range-set  := range ( "||" range )*
//	range      := comparator *( ws | "," ) comparator
//	comparator := op? partial
//	op         := "=" | "==" | "!=" | ">" | ">=" | "<" | "<=" | "^" | "~"
//	partial    := "X.Y.Z[-pre]"  | "X.Y" | "X" | "*" | "x"（含 1.x / 1.2.* 等形态）
func ParseConstraint(s string) (Constraint, error) {
	raw := s
	s = strings.TrimSpace(s)
	if s == "" {
		return Constraint{}, fmt.Errorf("%w: empty constraint", ErrInvalidConstraint)
	}
	for _, bad := range []string{"\n", "\t"} {
		if strings.Contains(s, bad) {
			return Constraint{}, fmt.Errorf("%w: control character", ErrInvalidConstraint)
		}
	}
	parts := strings.Split(s, "||")
	c := Constraint{Raw: raw}
	for _, p := range parts {
		g, err := parseGroup(strings.TrimSpace(p))
		if err != nil {
			return Constraint{}, err
		}
		c.groups = append(c.groups, g)
	}
	return c, nil
}

func parseGroup(s string) (cgroup, error) {
	s = strings.ReplaceAll(s, ",", " ")
	toks := strings.Fields(s)
	if len(toks) == 0 {
		return cgroup{}, fmt.Errorf("%w: empty range", ErrInvalidConstraint)
	}
	g := cgroup{gate: map[[3]int64]struct{}{}}
	for _, tok := range toks {
		op, rest, err := splitOp(tok)
		if err != nil {
			return cgroup{}, err
		}
		p, err := parsePartial(rest)
		if err != nil {
			return cgroup{}, err
		}
		comps, err := expand(op, p)
		if err != nil {
			return cgroup{}, err
		}
		for _, cmp := range comps {
			if cmp.V.IsPrerelease() {
				g.gate[cmp.V.Key()] = struct{}{}
			}
			g.comps = append(g.comps, cmp)
		}
	}
	return g, nil
}

func splitOp(tok string) (string, string, error) {
	for _, op := range []string{">=", "<=", "==", "!=", "^", "~", "=", ">", "<"} {
		if strings.HasPrefix(tok, op) {
			rest := strings.TrimSpace(tok[len(op):])
			if rest == "" {
				return "", "", fmt.Errorf("%w: operator %q without version", ErrInvalidConstraint, op)
			}
			return op, rest, nil
		}
	}
	return "", tok, nil
}

// partial 是带可选通配位的部分版本。n 为给定的数字段个数（0..3）。
type partial struct {
	major, minor, patch int64
	n                   int
	pre                 []ID
}

func parsePartial(s string) (partial, error) {
	prePart := ""
	if i := strings.IndexByte(s, '-'); i >= 0 {
		prePart = s[i+1:]
		s = s[:i]
	}
	fields := strings.Split(s, ".")
	if len(fields) > 3 {
		return partial{}, fmt.Errorf("%w: too many version segments in %q", ErrInvalidConstraint, s)
	}
	p := partial{}
	wildSeen := false
	for i, f := range fields {
		isWild := f == "*" || f == "x" || f == "X"
		if isWild {
			wildSeen = true
			continue
		}
		if wildSeen {
			return partial{}, fmt.Errorf("%w: numeric segment after wildcard in %q", ErrInvalidConstraint, s)
		}
		n, err := parseStrictUint(f)
		if err != nil {
			return partial{}, fmt.Errorf("%w: bad segment %q", ErrInvalidConstraint, f)
		}
		switch i {
		case 0:
			p.major = n
		case 1:
			p.minor = n
		case 2:
			p.patch = n
		}
		p.n = i + 1
	}
	if wildSeen && p.n == 0 && len(fields) > 1 {
		// 形如 "x.y"：第一段即通配，属于合法的全通配写法
		p.n = 0
	}
	if prePart != "" {
		if p.n != 3 || wildSeen {
			return partial{}, fmt.Errorf("%w: prerelease requires full X.Y.Z version in %q", ErrInvalidConstraint, s)
		}
		for _, seg := range strings.Split(prePart, ".") {
			id, err := parseID(seg)
			if err != nil {
				return partial{}, fmt.Errorf("%w: bad prerelease %q", ErrInvalidConstraint, seg)
			}
			p.pre = append(p.pre, id)
		}
	}
	return p, nil
}

func (p partial) ver() Version {
	return Version{Major: p.major, Minor: p.minor, Patch: p.patch, Prerelease: p.pre}
}

// expand 把“运算符 + 部分版本”展开成若干 AND 原子比较。
func expand(op string, p partial) ([]Comparator, error) {
	full := p.n == 3
	v := p.ver()
	switch op {
	case "", "=":
		switch p.n {
		case 0:
			return nil, nil // 任意
		case 1:
			return xRange(p.major), nil
		case 2:
			return []Comparator{
				{OpGte, Version{Major: p.major, Minor: p.minor}},
				{OpLt, Version{Major: p.major, Minor: p.minor + 1}},
			}, nil
		default:
			return []Comparator{{OpEq, v}}, nil
		}
	case "==":
		if !full {
			return nil, fmt.Errorf("%w: == requires full X.Y.Z", ErrInvalidConstraint)
		}
		return []Comparator{{OpEq, v}}, nil
	case "!=":
		if !full {
			return nil, fmt.Errorf("%w: != requires full X.Y.Z", ErrInvalidConstraint)
		}
		return []Comparator{{OpNeq, v}}, nil
	case "^":
		return caret(p)
	case "~":
		return tilde(p)
	case ">=":
		return []Comparator{{OpGte, p.ver()}}, nil
	case ">":
		switch p.n {
		case 0:
			// >* 退化为“没有上界”，即不产生限制
			return nil, nil
		case 1:
			return []Comparator{{OpGte, Version{Major: p.major + 1}}}, nil
		case 2:
			return []Comparator{{OpGte, Version{Major: p.major, Minor: p.minor + 1}}}, nil
		default:
			return []Comparator{{OpGt, v}}, nil
		}
	case "<=":
		switch p.n {
		case 0:
			return nil, nil
		case 1:
			return []Comparator{{OpLt, Version{Major: p.major + 1}}}, nil
		case 2:
			return []Comparator{{OpLt, Version{Major: p.major, Minor: p.minor + 1}}}, nil
		default:
			return []Comparator{{OpLte, v}}, nil
		}
	case "<":
		switch p.n {
		case 0:
			return nil, nil
		case 1:
			return []Comparator{{OpLt, Version{Major: p.major}}}, nil
		case 2:
			return []Comparator{{OpLt, Version{Major: p.major, Minor: p.minor}}}, nil
		default:
			return []Comparator{{OpLt, v}}, nil
		}
	default:
		return nil, fmt.Errorf("%w: unknown operator %q", ErrInvalidConstraint, op)
	}
}

// xRange 展开单段主版本（1 / 1.x）为 [1.0.0, 2.0.0)。
func xRange(major int64) []Comparator {
	return []Comparator{
		{OpGte, Version{Major: major}},
		{OpLt, Version{Major: major + 1}},
	}
}

// caret 实现简化的 npm 兼容 ^ 区间：
// 主版本>0 或只给定主版本时锁主版本；0.x 中给定次版本>0 或仅给 (0,minor) 时锁次版本；
// 其余 0.0.z 锁修订号。
func caret(p partial) ([]Comparator, error) {
	if p.n == 0 {
		return nil, fmt.Errorf("%w: ^ requires a version", ErrInvalidConstraint)
	}
	v := p.ver()
	var upper Version
	switch {
	case p.major > 0 || p.n == 1:
		upper = Version{Major: p.major + 1}
	case p.minor > 0 || p.n == 2:
		upper = Version{Major: 0, Minor: p.minor + 1}
	default:
		upper = Version{Major: 0, Minor: 0, Patch: p.patch + 1}
	}
	return []Comparator{{OpGte, v}, {OpLt, upper}}, nil
}

// tilde 实现简化的 npm 兼容 ~ 区间。
func tilde(p partial) ([]Comparator, error) {
	if p.n == 0 {
		return nil, fmt.Errorf("%w: ~ requires a version", ErrInvalidConstraint)
	}
	v := p.ver()
	var upper Version
	switch p.n {
	case 1:
		upper = Version{Major: p.major + 1}
	default: // 2 或 3：锁定次版本
		upper = Version{Major: p.major, Minor: p.minor + 1}
	}
	return []Comparator{{OpGte, v}, {OpLt, upper}}, nil
}

// Satisfy 报告给定版本是否满足本约束。
// allowPrerelease=false 时启用简化 npm 预发布门控（见包文档/README）。
func (c Constraint) Satisfy(v Version, allowPrerelease bool) bool {
	for _, g := range c.groups {
		if groupSatisfy(g, v, allowPrerelease) {
			return true
		}
	}
	return false
}

func groupSatisfy(g cgroup, v Version, allowPrerelease bool) bool {
	if v.IsPrerelease() && !allowPrerelease {
		if _, ok := g.gate[v.Key()]; !ok {
			return false
		}
	}
	for _, cmp := range g.comps {
		if !cmp.eval(v) {
			return false
		}
	}
	return true
}

func (cmp Comparator) eval(v Version) bool {
	r := Compare(v, cmp.V)
	switch cmp.Op {
	case OpEq:
		return r == 0
	case OpNeq:
		return r != 0
	case OpGt:
		return r > 0
	case OpGte:
		return r >= 0
	case OpLt:
		return r < 0
	case OpLte:
		return r <= 0
	default:
		return false
	}
}

// MentionsPrerelease 报告约束是否显式提及给定 (major,minor,patch) 基元的预发布版本，
// 供简化 npm 预发布门控使用（例如 >=1.0.0-beta.1 显式提及 1.0.0）。
func (c Constraint) MentionsPrerelease(key [3]int64) bool {
	for _, g := range c.groups {
		if _, ok := g.gate[key]; ok {
			return true
		}
	}
	return false
}

// String 返回规范化的约束展示串。
func (c Constraint) String() string {
	var gs []string
	for _, g := range c.groups {
		var cs []string
		for _, cmp := range g.comps {
			cs = append(cs, string(cmp.Op)+cmp.V.String())
		}
		if len(cs) == 0 {
			cs = []string{"*"}
		}
		gs = append(gs, strings.Join(cs, " "))
	}
	return strings.Join(gs, " || ")
}
