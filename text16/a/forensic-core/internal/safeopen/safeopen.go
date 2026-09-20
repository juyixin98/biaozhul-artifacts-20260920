// Package safeopen 在白名单根目录内安全地解析并只读打开证据镜像，
// 防止路径穿越与符号链接越界。
package safeopen

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"forensiccore/internal/hashutil"
)

var (
	// ErrOutsideRoot 路径解析后落在白名单根目录之外。
	ErrOutsideRoot = errors.New("path escapes evidence root")
	// ErrBadExtension 只接受 .raw / .dd 镜像。
	ErrBadExtension = errors.New("only .raw and .dd image files are accepted")
	// ErrNotRegular 目标不是普通文件。
	ErrNotRegular = errors.New("not a regular file")
	// ErrRace 打开过程中文件被替换（lstat 与 open 结果不一致）。
	ErrRace = errors.New("file changed between stat and open")
)

var allowedExts = map[string]bool{".raw": true, ".dd": true}

// Root 已解析（绝对、去符号链接）的白名单根目录。
type Root struct {
	root string
}

// NewRoot 创建根目录句柄。目录必须存在。
func NewRoot(dir string) (*Root, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, err
	}
	resolved, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return nil, fmt.Errorf("resolve evidence root: %w", err)
	}
	st, err := os.Stat(resolved)
	if err != nil {
		return nil, fmt.Errorf("stat evidence root: %w", err)
	}
	if !st.IsDir() {
		return nil, fmt.Errorf("evidence root %s is not a directory", resolved)
	}
	return &Root{root: resolved}, nil
}

// Path 返回根目录绝对路径。
func (r *Root) Path() string { return r.root }

// within 判断 p 是否位于根目录内。
func (r *Root) within(p string) bool {
	return p == r.root || strings.HasPrefix(p, r.root+string(os.PathSeparator))
}

// Resolve 校验相对路径并返回根目录内的绝对路径。
// 拒绝：绝对路径、".." 穿越、符号链接逃逸、非 .raw/.dd 扩展名。
func (r *Root) Resolve(rel string) (string, error) {
	if rel == "" || filepath.IsAbs(rel) {
		return "", ErrOutsideRoot
	}
	cleaned := filepath.Clean(rel)
	if cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(os.PathSeparator)) {
		return "", ErrOutsideRoot
	}
	full := filepath.Join(r.root, cleaned)
	// EvalSymlinks 解析整条路径（含中间目录），逃逸即拒绝。
	resolved, err := filepath.EvalSymlinks(full)
	if err != nil {
		return "", err
	}
	if !r.within(resolved) {
		return "", ErrOutsideRoot
	}
	if !allowedExts[strings.ToLower(filepath.Ext(resolved))] {
		return "", ErrBadExtension
	}
	return resolved, nil
}

// OpenReadOnly 解析路径并以 O_RDONLY 打开，返回文件句柄与打开时刻的身份指纹。
// 通过 lstat/open 一致性检查与 /proc/self/fd 复查抵御符号链接竞争。
func (r *Root) OpenReadOnly(rel string) (*os.File, hashutil.Identity, error) {
	resolved, err := r.Resolve(rel)
	if err != nil {
		return nil, hashutil.Identity{}, err
	}
	fi, err := os.Lstat(resolved)
	if err != nil {
		return nil, hashutil.Identity{}, err
	}
	if !fi.Mode().IsRegular() {
		return nil, hashutil.Identity{}, ErrNotRegular
	}
	f, err := os.OpenFile(resolved, os.O_RDONLY, 0)
	if err != nil {
		return nil, hashutil.Identity{}, err
	}
	fail := func(err error) (*os.File, hashutil.Identity, error) {
		f.Close()
		return nil, hashutil.Identity{}, err
	}
	fst, err := f.Stat()
	if err != nil {
		return fail(err)
	}
	if !os.SameFile(fi, fst) {
		return fail(ErrRace)
	}
	// Linux 下复查已打开 fd 的真实路径仍在根目录内，关闭 stat→open 之间的竞争窗口。
	if target, err := os.Readlink(fmt.Sprintf("/proc/self/fd/%d", f.Fd())); err == nil {
		if !r.within(strings.TrimSuffix(target, " (deleted)")) {
			return fail(ErrOutsideRoot)
		}
	}
	id, err := hashutil.IdentityOf(f)
	if err != nil {
		return fail(err)
	}
	return f, id, nil
}
