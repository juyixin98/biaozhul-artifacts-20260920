// Package resolver 按照 C 预处理器规则把 #include 中出现的逻辑路径
// 解析为文件系统中的绝对路径。
//
// 搜索顺序（与典型 C 编译器一致）：
//
//   - 引号形式（#include "x"）：
//     1. 当前包含文件所在目录（相对包含者目录）；
//     2. 调用方配置的引号搜索目录（quoteDirs）；
//     3. 系统搜索目录（systemDirs，即 -I 风格目录）。
//   - 尖括号形式（#include <x>）：仅搜索系统搜索目录。
//
// 同名头文件即借此区分：不同目录下的同名文件解析为不同绝对路径，
// 图中按绝对路径唯一标识节点。
package resolver

import (
	"os"
	"path/filepath"
	"sort"
	"strings"

	"depscanner/internal/scanner"
)

// Resolver 维护解析所需的搜索目录集合。
type Resolver struct {
	quoteDirs  []string // 绝对路径，引号形式附加搜索目录
	systemDirs []string // 绝对路径，尖括号（及引号兜底）搜索目录
}

// Resolved 描述一次成功解析的结果。
type Resolved struct {
	// Abs 为命中文件的绝对路径。
	Abs string
	// SearchDir 为命中时所用的搜索目录（相对包含者目录时为该目录本身）。
	SearchDir string
	// Scope 为命中来源："relative"（相对包含者）、"quote"、"system"、"external"。
	Scope string
}

// New 创建解析器。root 为项目根目录（用于把相对搜索目录解析为绝对路径）。
// quoteDirs/systemDirs 中的相对路径相对于 root 解释。
func New(root string, quoteDirs, systemDirs []string) (*Resolver, error) {
	r := &Resolver{}
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	add := func(list []string) ([]string, error) {
		var out []string
		for _, d := range list {
			if filepath.IsAbs(d) {
				d = filepath.Clean(d)
			} else {
				d = filepath.Clean(filepath.Join(rootAbs, d))
			}
			// 与 C 编译器一致：不存在的搜索目录静默忽略。
			if !isDir(d) {
				continue
			}
			out = append(out, d)
		}
		return out, nil
	}
	if r.quoteDirs, err = add(quoteDirs); err != nil {
		return nil, err
	}
	if r.systemDirs, err = add(systemDirs); err != nil {
		return nil, err
	}
	return r, nil
}

// QuoteDirs 返回配置的引号搜索目录（绝对路径、保持配置顺序）。
func (r *Resolver) QuoteDirs() []string { return append([]string(nil), r.quoteDirs...) }

// SystemDirs 返回配置的系统搜索目录（绝对路径、保持配置顺序）。
func (r *Resolver) SystemDirs() []string { return append([]string(nil), r.systemDirs...) }

func isDir(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

// Resolve 按规则解析单条 include。includingFile 为包含者文件的绝对路径；
// includePath 为绝对路径（绝对 include 直接命中判定，不查搜索目录）时直接检查存在性。
// 解析失败（所有候选均不存在）返回 ok=false。
func (r *Resolver) Resolve(includingFile string, inc scanner.Include) (res Resolved, ok bool) {
	target := filepath.Clean(inc.Path)
	if filepath.IsAbs(target) {
		if fi, err := os.Stat(target); err == nil && !fi.IsDir() {
			return Resolved{Abs: target, SearchDir: filepath.Dir(target), Scope: "absolute"}, true
		}
		return Resolved{}, false
	}

	var dirs []struct {
		dir   string
		scope string
	}
	if inc.Kind == scanner.Quoted {
		dirs = append(dirs, struct {
			dir   string
			scope string
		}{filepath.Dir(includingFile), "relative"})
		for _, d := range r.quoteDirs {
			dirs = append(dirs, struct {
				dir   string
				scope string
			}{d, "quote"})
		}
	}
	for _, d := range r.systemDirs {
		dirs = append(dirs, struct {
			dir   string
			scope string
		}{d, "system"})
	}

	for _, cand := range dirs {
		p := filepath.Join(cand.dir, target)
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			return Resolved{Abs: p, SearchDir: cand.dir, Scope: cand.scope}, true
		}
	}
	return Resolved{}, false
}

// FirstCandidate 返回该 include 在搜索顺序下的首个候选绝对路径
// （无论文件是否存在）。用于为无法解析的 include 生成稳定的缺失节点键。
func (r *Resolver) FirstCandidate(includingFile string, inc scanner.Include) string {
	target := filepath.Clean(inc.Path)
	if filepath.IsAbs(target) {
		return target
	}
	var firstDir string
	switch {
	case inc.Kind == scanner.Quoted:
		firstDir = filepath.Dir(includingFile)
	case len(r.systemDirs) > 0:
		firstDir = r.systemDirs[0]
	case len(r.quoteDirs) > 0:
		firstDir = r.quoteDirs[0]
	default:
		firstDir = filepath.Dir(includingFile)
	}
	return filepath.Join(firstDir, target)
}

// IsUnder 报告 path 是否位于 dir 目录之下（或等于 dir）。
// 两者都会先清洗为绝对路径，避免 /a/b 与 /a/bb 之类前缀误判。
func IsUnder(dir, path string) bool {
	d, err := filepath.Abs(filepath.Clean(dir))
	if err != nil {
		return false
	}
	p, err := filepath.Abs(filepath.Clean(path))
	if err != nil {
		return false
	}
	if p == d {
		return true
	}
	rel, err := filepath.Rel(d, p)
	if err != nil {
		return false
	}
	return rel != "." && rel != ".." &&
		!strings.HasPrefix(rel, ".."+string(filepath.Separator)) &&
		!filepath.IsAbs(rel)
}

// SortedUnique 按字典序去重，供输出使用。
func SortedUnique(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := in[:0]
	for _, s := range in {
		if _, dup := seen[s]; dup {
			continue
		}
		seen[s] = struct{}{}
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}
