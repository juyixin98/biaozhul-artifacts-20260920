package solver_test

// 本文件验证项目的离线边界：除本地 HTTP 服务（net/http 的服务端用途）外，
// 代码库不得出现任何出站网络能力。它是一个静态守卫测试——若有人引入
// http.Get/http.Post/http.Client/Dial 等调用，测试会失败并提醒评审。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNoOutboundNetworkCalls(t *testing.T) {
	root := filepath.Join("..", "..")
	// 允许出现的 net/http 符号仅限服务端构造（ListenAndServe / ServeMux / Handler）。
	forbidden := []string{
		"http.Get(", "http.Post(", "http.Head(", "http.NewRequest(",
		"http.DefaultClient", "&http.Client{", "http.Client{",
		"net.Dial(", "net.DialTCP(", "net.DialUDP(",
		"grpc.Dial", "sql.Open(",
	}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			name := info.Name()
			if name == ".git" || name == "docs" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		src := string(data)
		for _, bad := range forbidden {
			if strings.Contains(src, bad) {
				t.Errorf("%s: forbidden outbound-network symbol %q — this project resolves from request-local data only",
					path, bad)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
