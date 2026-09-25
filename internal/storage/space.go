package storage

import (
	"fmt"
	"syscall"
)

// ErrNoSpace 表示磁盘空间不足，本次操作被拒绝；已有制品不受影响。
var ErrNoSpace = fmt.Errorf("磁盘空间不足")

// FreeBytes 返回 path 所在文件系统的可用字节数。
// cap 为 0 表示不限制；否则可用空间按 min(实际, cap) 计算，
// 用于测试模拟小磁盘。
func FreeBytes(path string, cap uint64) (uint64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, err
	}
	free := st.Bavail * uint64(st.Bsize)
	if cap > 0 && cap < free {
		free = cap
	}
	return free, nil
}

// CheckSpace 校验 path 所在文件系统是否至少有 need 字节可用空间。
func CheckSpace(path string, need uint64, cap uint64) error {
	free, err := FreeBytes(path, cap)
	if err != nil {
		return err
	}
	if free < need {
		return fmt.Errorf("%w: 需要 %d 字节，可用 %d 字节", ErrNoSpace, need, free)
	}
	return nil
}
