package engine

import (
	"io/fs"
	"os"
)

// statFile 只允许普通文件或目录，拒绝符号链接等特殊类型。
func statFile(path string) (fs.FileInfo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() && !info.IsDir() {
		return nil, &fs.PathError{Op: "stat", Path: path, Err: fs.ErrInvalid}
	}
	return info, nil
}
