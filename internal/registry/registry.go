// Package registry 把 JSON 注册表编译成校验过、带索引的内存结构。
// 本项目的“注册表”完全来自请求或本地夹具，绝不联网拉取。
package registry

import (
	"fmt"
	"regexp"
	"sort"

	"depsolver/internal/model"
	"depsolver/internal/semver"
)

// namePattern 是本项目接受的包名集合：小写字母数字、. _ -，可含单层命名空间 scope/name。
var namePattern = regexp.MustCompile(`^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$`)

// ValidName 报告包名是否合法。
func ValidName(name string) bool { return namePattern.MatchString(name) }

// ParsedVersion 是解析后的版本条目。
type ParsedVersion struct {
	V    semver.Version
	Raw  string
	Deps []ParsedDep
}

// ParsedDep 是依赖声明的解析形态。
type ParsedDep struct {
	Name       string
	Constraint semver.Constraint
	Raw        string
}

// PackageEntry 是单个包的索引：版本按语义版本降序排列。
type PackageEntry struct {
	Name     string
	Versions []ParsedVersion // 降序：新版本在前
}

// Registry 是编译后的只读注册表。
type Registry struct {
	pkgs map[string]*PackageEntry
}

// Build 校验并编译输入注册表：
//   - 包名合法、包不重复；
//   - 每个版本字符串是合法 SemVer、同包内版本不重复；
//   - 每条依赖的约束可解析（依赖的包是否存在推迟到求解期判定）；
//   - 版本按语义版本降序排列，保证求解候选顺序确定。
func Build(in model.RegistryInput) (*Registry, error) {
	r := &Registry{pkgs: map[string]*PackageEntry{}}
	for _, pkg := range in.Packages {
		if !ValidName(pkg.Name) {
			return nil, fmt.Errorf("invalid package name %q", pkg.Name)
		}
		if _, dup := r.pkgs[pkg.Name]; dup {
			return nil, fmt.Errorf("duplicate package %q", pkg.Name)
		}
		entry := &PackageEntry{Name: pkg.Name}
		seen := map[string]struct{}{}
		for _, pv := range pkg.Versions {
			v, err := semver.Parse(pv.Version)
			if err != nil {
				return nil, fmt.Errorf("package %q: invalid version %q: %w", pkg.Name, pv.Version, err)
			}
			if _, dup := seen[pv.Version]; dup {
				return nil, fmt.Errorf("package %q: duplicate version %q", pkg.Name, pv.Version)
			}
			seen[pv.Version] = struct{}{}
			pvParsed := ParsedVersion{V: v, Raw: pv.Version}
			depNames := map[string]struct{}{}
			for _, d := range pv.Deps {
				if !ValidName(d.Name) {
					return nil, fmt.Errorf("package %q version %s: invalid dependency name %q", pkg.Name, pv.Version, d.Name)
				}
				c, err := semver.ParseConstraint(d.Constraint)
				if err != nil {
					return nil, fmt.Errorf("package %q version %s dep %q: %w", pkg.Name, pv.Version, d.Name, err)
				}
				if _, dup := depNames[d.Name]; dup {
					return nil, fmt.Errorf("package %q version %s: duplicate dependency %q", pkg.Name, pv.Version, d.Name)
				}
				depNames[d.Name] = struct{}{}
				pvParsed.Deps = append(pvParsed.Deps, ParsedDep{Name: d.Name, Constraint: c, Raw: d.Constraint})
			}
			entry.Versions = append(entry.Versions, pvParsed)
		}
		sort.Slice(entry.Versions, func(i, j int) bool {
			return semver.Compare(entry.Versions[i].V, entry.Versions[j].V) > 0
		})
		r.pkgs[pkg.Name] = entry
	}
	return r, nil
}

// Package 返回包条目；不存在返回 nil。
func (r *Registry) Package(name string) *PackageEntry { return r.pkgs[name] }

// Names 返回全部包名（升序，供穷举等需要确定性的调用方使用）。
func (r *Registry) Names() []string {
	names := make([]string, 0, len(r.pkgs))
	for n := range r.pkgs {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}
