package safeopen

import "errors"

// 路径/文件安全检查错误。
var (
	// ErrPathTraversal 相对路径非法：绝对路径、含 ".."/"."/空分量。
	ErrPathTraversal = errors.New("illegal relative path: traversal components are not allowed")
	// ErrSymlink 路径某一级是符号链接（已被 O_NOFOLLOW 拒绝）。
	ErrSymlink = errors.New("symlink is not allowed inside the evidence root")
	// ErrNotRegular 目标不是常规文件。
	ErrNotRegular = errors.New("evidence path is not a regular file")
	// ErrNotDirectory 中间分量不是目录。
	ErrNotDirectory = errors.New("a path component is not a directory")
	// ErrNotFound 文件不存在。
	ErrNotFound = errors.New("evidence file not found")
	// ErrPermission 权限不足。
	ErrPermission = errors.New("permission denied")
	// ErrUnsupportedPlatform 非 Linux 平台不支持。
	ErrUnsupportedPlatform = errors.New("safeopen is only implemented on Linux")
)

// FileStat 是平台无关的文件身份信息。Dev/Ino 与 Size 一起用于判断
// “是不是同一个文件”，不能只靠文件名或大小。
type FileStat struct {
	Dev   uint64
	Ino   uint64
	Mode  uint32
	Size  int64
	Mtime int64 // 纳秒
	Nlink uint64
}
