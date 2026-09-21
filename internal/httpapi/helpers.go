package httpapi

import (
	"fmt"
	"io"
	"net/url"
)

// copyBody 把存储文件内容拷贝到响应体。
func copyBody(w io.Writer, r io.Reader) (int64, error) { return io.Copy(w, r) }

// contentDisposition 生成尽量兼容中文文件名的 Content-Disposition。
func contentDisposition(name string) string {
	return fmt.Sprintf("attachment; filename*=UTF-8''%s", url.PathEscape(name))
}
