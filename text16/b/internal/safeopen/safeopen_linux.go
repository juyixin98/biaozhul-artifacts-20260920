//go:build linux

package safeopen

import (
	"golang.org/x/sys/unix"
)

// Root 是一个白名单根目录的长期打开句柄。所有证据文件都必须通过 Root.Open
// 访问：逐级 openat 解析保证不可能逃出根目录，O_NOFOLLOW 保证路径任何一级
// 都不是符号链接。
type Root struct {
	Name string
	Path string
	fd   int
}

// OpenRoot 打开并持有白名单根目录。根目录本身由运维配置（可来自挂载点），
// 允许是符号链接，内核会解析到真实目录。
func OpenRoot(name, path string) (*Root, error) {
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	return &Root{Name: name, Path: path, fd: fd}, nil
}

// Close 释放根目录句柄。
func (r *Root) Close() error {
	if r.fd != 0 {
		return unix.Close(r.fd)
	}
	return nil
}

// Open 以 flags（如 unix.O_RDONLY）打开根目录下 relPath 指向的普通文件。
//
// 安全保证：
//   - relPath 必须是相对路径，不得含 ".."、"."、空分量或绝对路径；
//   - 逐级 openat(O_PATH|O_NOFOLLOW|O_DIRECTORY) 打开中间目录，符号链接
//     在任何一级都会以 ELOOP 失败，无法用链接逃出根；
//   - 末级以 O_NOFOLLOW 打开并 fstat 校验为常规文件；
//   - 句柄基于文件描述符，打开后即便路径被替换为符号链接也不影响已打开文件。
func (r *Root) Open(relPath string, flags int) (int, *FileStat, error) {
	components, err := splitRelPath(relPath)
	if err != nil {
		return 0, nil, err
	}

	dirFD := r.fd
	opened := make([]int, 0, len(components)-1)
	closeAll := func() {
		for _, fd := range opened {
			_ = unix.Close(fd)
		}
	}

	// 逐级打开中间目录。
	for i := 0; i < len(components)-1; i++ {
		fd, err := unix.Openat(dirFD, components[i],
			unix.O_PATH|unix.O_NOFOLLOW|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
		if err == nil {
			opened = append(opened, fd)
			dirFD = fd
			continue
		}
		if err == unix.ELOOP {
			closeAll()
			return 0, nil, ErrSymlink
		}
		if err == unix.ENOTDIR {
			closeAll()
			return 0, nil, ErrNotDirectory
		}
		closeAll()
		return 0, nil, classifyErr(err)
	}

	// 末级打开真正的文件：只读、拒绝跟随符号链接。
	fd, err := unix.Openat(dirFD, components[len(components)-1],
		flags|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		if err == unix.ELOOP {
			closeAll()
			return 0, nil, ErrSymlink
		}
		closeAll()
		return 0, nil, classifyErr(err)
	}

	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		_ = unix.Close(fd)
		closeAll()
		return 0, nil, err
	}
	// 只接受常规文件（排除设备、fifo、socket 等）。
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		_ = unix.Close(fd)
		closeAll()
		return 0, nil, ErrNotRegular
	}
	// 中间目录句柄已不再需要（末级文件句柄独立存活）。
	closeAll()
	return fd, &FileStat{
		Dev:   st.Dev,
		Ino:   st.Ino,
		Mode:  st.Mode,
		Size:  st.Size,
		Mtime: st.Mtim.Nano(),
		Nlink: uint64(st.Nlink),
	}, nil
}

func classifyErr(err error) error {
	switch err {
	case unix.ENOENT:
		return ErrNotFound
	case unix.EACCES:
		return ErrPermission
	default:
		return err
	}
}
