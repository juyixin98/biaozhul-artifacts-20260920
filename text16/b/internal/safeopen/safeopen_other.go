//go:build !linux

package safeopen

// Root 在非 Linux 平台为占位实现。生产部署目标为 Linux（容器）。
type Root struct {
	Name string
	Path string
}

// OpenRoot 在非 Linux 平台返回错误，提示应在 Linux 上运行。
func OpenRoot(name, path string) (*Root, error) {
	return nil, ErrUnsupportedPlatform
}

// Close 为占位实现。
func (r *Root) Close() error { return nil }

// Open 在非 Linux 平台不可用。
func (r *Root) Open(relPath string, flags int) (int, *FileStat, error) {
	return 0, nil, ErrUnsupportedPlatform
}
