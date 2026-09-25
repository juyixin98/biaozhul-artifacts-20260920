// Package semver 实现简化语义版本（Semantic Versioning 2.0 的严格子集）。
//
// 支持的形态：主版本.次版本.修订号[-预发布][+构建元数据]，例如
//
//	1.2.3
//	1.2.3-beta.1
//	1.2.3+build.7
//
// 不支持 "v" 前缀、不支持四段数字；预发布标识遵循 SemVer 2.0 的比较规则
// （数字标识按数值比较、字母标识按 ASCII 字典序比较、数字标识小于字母标识、
// 无预发布大于有预发布）。构建元数据允许解析但不参与比较。
package semver

import (
	"errors"
	"strconv"
	"strings"
)

// ErrInvalidVersion 在版本字符串不符合简化 SemVer 语法时返回。
var ErrInvalidVersion = errors.New("invalid semantic version")

// Version 是解析后的语义版本。
type Version struct {
	Major      int64
	Minor      int64
	Patch      int64
	Prerelease []ID
	Build      []string // 构建元数据，仅保留用于字符串还原，不参与比较
}

// ID 是预发布片段：数字标识走数值比较，否则走 ASCII 字符串比较。
type ID struct {
	Num   int64
	Str   string
	IsNum bool
}

// Parse 解析严格的 X.Y.Z[-prerelease][+build] 形态。
func Parse(s string) (Version, error) {
	if s == "" {
		return Version{}, ErrInvalidVersion
	}
	build := ""
	if i := strings.IndexByte(s, '+'); i >= 0 {
		build = s[i+1:]
		s = s[:i]
		if build == "" {
			return Version{}, ErrInvalidVersion
		}
	}
	pre := ""
	if i := strings.IndexByte(s, '-'); i >= 0 {
		pre = s[i+1:]
		s = s[:i]
		if pre == "" {
			return Version{}, ErrInvalidVersion
		}
	}
	parts := strings.Split(s, ".")
	if len(parts) != 3 {
		return Version{}, ErrInvalidVersion
	}
	nums := make([]int64, 3)
	for i, p := range parts {
		n, err := parseStrictUint(p)
		if err != nil {
			return Version{}, ErrInvalidVersion
		}
		nums[i] = n
	}
	var preIDs []ID
	if pre != "" {
		for _, seg := range strings.Split(pre, ".") {
			if seg == "" {
				return Version{}, ErrInvalidVersion
			}
			id, err := parseID(seg)
			if err != nil {
				return Version{}, ErrInvalidVersion
			}
			preIDs = append(preIDs, id)
		}
	}
	var buildSegs []string
	if build != "" {
		for _, seg := range strings.Split(build, ".") {
			if seg == "" || !isAlnumHyphen(seg) {
				return Version{}, ErrInvalidVersion
			}
			buildSegs = append(buildSegs, seg)
		}
	}
	return Version{Major: nums[0], Minor: nums[1], Patch: nums[2], Prerelease: preIDs, Build: buildSegs}, nil
}

// parseStrictUint 拒绝前导零（单独的 "0" 允许）与非数字内容。
func parseStrictUint(s string) (int64, error) {
	if s == "" || !allDigits(s) {
		return 0, ErrInvalidVersion
	}
	if len(s) > 1 && s[0] == '0' {
		return 0, ErrInvalidVersion
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, ErrInvalidVersion
	}
	return n, nil
}

func parseID(s string) (ID, error) {
	if s == "" || !isAlnumHyphen(s) {
		return ID{}, ErrInvalidVersion
	}
	if allDigits(s) {
		if len(s) > 1 && s[0] == '0' {
			// SemVer 2.0：数字标识禁止前导零
			return ID{}, ErrInvalidVersion
		}
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return ID{}, ErrInvalidVersion
		}
		return ID{Num: n, IsNum: true}, nil
	}
	return ID{Str: s}, nil
}

func allDigits(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return len(s) > 0
}

func isAlnumHyphen(s string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c == '-') {
			return false
		}
	}
	return true
}

// IsPrerelease 报告版本是否携带预发布标识。
func (v Version) IsPrerelease() bool { return len(v.Prerelease) > 0 }

// String 还原规范化字符串（保留构建元数据）。
func (v Version) String() string {
	var b strings.Builder
	b.WriteString(strconv.FormatInt(v.Major, 10))
	b.WriteByte('.')
	b.WriteString(strconv.FormatInt(v.Minor, 10))
	b.WriteByte('.')
	b.WriteString(strconv.FormatInt(v.Patch, 10))
	for i, id := range v.Prerelease {
		if i == 0 {
			b.WriteByte('-')
		} else {
			b.WriteByte('.')
		}
		if id.IsNum {
			b.WriteString(strconv.FormatInt(id.Num, 10))
		} else {
			b.WriteString(id.Str)
		}
	}
	if len(v.Build) > 0 {
		b.WriteByte('+')
		b.WriteString(strings.Join(v.Build, "."))
	}
	return b.String()
}

// Compare 严格按 SemVer 优先级比较：-1 / 0 / 1。
func Compare(a, b Version) int {
	if c := cmpInt(a.Major, b.Major); c != 0 {
		return c
	}
	if c := cmpInt(a.Minor, b.Minor); c != 0 {
		return c
	}
	if c := cmpInt(a.Patch, b.Patch); c != 0 {
		return c
	}
	return comparePrerelease(a.Prerelease, b.Prerelease)
}

func comparePrerelease(a, b []ID) int {
	// 无预发布的版本优先级更高
	if len(a) == 0 && len(b) == 0 {
		return 0
	}
	if len(a) == 0 {
		return 1
	}
	if len(b) == 0 {
		return -1
	}
	for i := 0; i < len(a) && i < len(b); i++ {
		if c := compareID(a[i], b[i]); c != 0 {
			return c
		}
	}
	return cmpInt(int64(len(a)), int64(len(b)))
}

func compareID(a, b ID) int {
	switch {
	case a.IsNum && b.IsNum:
		return cmpInt(a.Num, b.Num)
	case a.IsNum && !b.IsNum:
		return -1 // 数字标识小于字母标识
	case !a.IsNum && b.IsNum:
		return 1
	default:
		return strings.Compare(a.Str, b.Str)
	}
}

func cmpInt(a, b int64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}

// Equal 比较版本（忽略构建元数据）。
func Equal(a, b Version) bool { return Compare(a, b) == 0 }

// Key 返回按 (major,minor,patch) 标识的稳定版本基元键，供预发布门控使用。
func (v Version) Key() [3]int64 { return [3]int64{v.Major, v.Minor, v.Patch} }
