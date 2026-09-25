package archive

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// writeFile 是测试辅助：写文件并设置 mtime/权限。
func writeFile(t *testing.T, path, content string, mode os.FileMode, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

func mkdir(t *testing.T, path string, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(path, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

func symlink(t *testing.T, target, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
}

// buildTar 调用 WriteTar 返回字节与摘要。
func buildTar(t *testing.T, src string, opts Options) ([]byte, *Summary) {
	t.Helper()
	var buf bytes.Buffer
	sum, err := WriteTar(src, &buf, opts)
	if err != nil {
		t.Fatalf("WriteTar 失败: %v", err)
	}
	return buf.Bytes(), sum
}

// readTarEntries 解析 tar 为 name -> header（内容已消费）。
func readTarEntries(t *testing.T, data []byte) []*tar.Header {
	t.Helper()
	r := tar.NewReader(bytes.NewReader(data))
	var hdrs []*tar.Header
	for {
		hdr, err := r.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatalf("解析 tar 失败: %v", err)
		}
		if _, err := io.Copy(io.Discard, r); err != nil {
			t.Fatal(err)
		}
		h := *hdr
		hdrs = append(hdrs, &h)
	}
	return hdrs
}

// TestDeterminismAcrossTraversalOrder 是核心验收：
// 两次构建把目录遍历顺序强制反转，tar 字节必须完全一致。
func TestDeterminismAcrossTraversalOrder(t *testing.T) {
	root := t.TempDir()
	mkFixture(t, root, time.Unix(946684800, 0)) // 2000-01-01

	data1, sum1 := withReversedReadir(t, false, func() ([]byte, *Summary) {
		return buildTar(t, root, Options{})
	})
	data2, sum2 := withReversedReadir(t, true, func() ([]byte, *Summary) {
		return buildTar(t, root, Options{})
	})

	if !bytes.Equal(data1, data2) {
		i := firstDiffIndex(data1, data2)
		t.Fatalf("不同遍历顺序产出的 tar 字节不一致，首个差异偏移=%d (len1=%d len2=%d)",
			i, len(data1), len(data2))
	}
	if sum1.ArtifactSHA256 != sum2.ArtifactSHA256 {
		t.Fatalf("摘要哈希不一致: %s != %s", sum1.ArtifactSHA256, sum2.ArtifactSHA256)
	}
}

// firstDiffIndex 返回两个字节流首个差异偏移；完全相同返回 -1。
func firstDiffIndex(a, b []byte) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return i
		}
	}
	if len(a) != len(b) {
		return n
	}
	return -1
}

// withReversedReadir 临时替换 readDirNames 接缝。
func withReversedReadir(t *testing.T, reverse bool, fn func() ([]byte, *Summary)) ([]byte, *Summary) {
	t.Helper()
	orig := readDirNames
	t.Cleanup(func() { readDirNames = orig })
	if reverse {
		readDirNames = func(dir string) ([]string, error) {
			names, err := orig(dir)
			if err != nil {
				return nil, err
			}
			out := make([]string, len(names))
			for i, n := range names {
				out[len(names)-1-i] = n
			}
			return out, nil
		}
	}
	return fn()
}

// mkFixture 建立覆盖 Unicode 路径、空目录、长文件名、深层目录、可执行位、
// 同内容多文件与符号链接（合法）的测试树。
func mkFixture(t *testing.T, root string, base time.Time) {
	t.Helper()
	mkdir(t, filepath.Join(root, "空目录-测试"), base)
	mkdir(t, filepath.Join(root, "d e é p", "子目录"), base)
	writeFile(t, filepath.Join(root, "a.txt"), "alpha\n", 0o600, base)
	writeFile(t, filepath.Join(root, "z.txt"), "alpha\n", 0o644, base.Add(2*time.Hour)) // 同内容不同 mtime/权限
	writeFile(t, filepath.Join(root, "d e é p", "b-ünïcode-文件.txt"), "beta\n", 0o644, base.Add(-time.Hour))
	writeFile(t, filepath.Join(root, "d e é p", "子目录", "γ.txt"), "gamma\n", 0o644, base.Add(48*time.Hour))
	writeFile(t, filepath.Join(root, "脚本.sh"), "#!/bin/sh\necho hi\n", 0o755, base.Add(3*time.Hour))
	// 长文件名：归档内路径超过 USTAR 100 字节限制（触发 PAX 扩展头），
	// 但单个名字段控制在文件系统 255 字节上限以内。
	longName := "long-" + strings.Repeat("あ", 70) + ".txt"
	writeFile(t, filepath.Join(root, longName), "long-content\n", 0o644, base.Add(5*time.Hour))
	// 合法符号链接：指向树内文件、树内目录以及悬空链接。
	symlink(t, "a.txt", filepath.Join(root, "link-to-a"))
	symlink(t, filepath.Join("d e é p"), filepath.Join(root, "link-to-dir"))
	symlink(t, filepath.Join("missing", "未来文件"), filepath.Join(root, "dangling-link"))
	// 子目录内使用 ../ 引用兄弟文件——词法合法，不得误判为逃逸。
	symlink(t, filepath.Join("..", "a.txt"), filepath.Join(root, "d e é p", "up-to-a"))
}

// TestDifferentMTimesSameBytes：两份 mtime 完全不同的目录树（相同内容/结构），
// 产出字节一致——这是“内容相同”的可复现语义。
func TestDifferentMTimesSameBytes(t *testing.T) {
	t1 := time.Unix(1000000000, 123456789)
	t2 := time.Unix(1700000000, 987654321)
	root1 := t.TempDir()
	root2 := t.TempDir()
	mkFixture(t, root1, t1)
	mkFixture(t, root2, t2)

	d1, s1 := buildTar(t, root1, Options{})
	d2, s2 := buildTar(t, root2, Options{})
	if !bytes.Equal(d1, d2) {
		t.Fatalf("不同 mtime 的等价目录树 tar 字节不一致")
	}
	if s1.ArtifactSHA256 != s2.ArtifactSHA256 {
		t.Fatalf("哈希不一致: %s vs %s", s1.ArtifactSHA256, s2.ArtifactSHA256)
	}
}

// TestFixedHeaderFields 校验固定时间、权限策略、属主清零与路径排序。
func TestFixedHeaderFields(t *testing.T) {
	root := t.TempDir()
	mkFixture(t, root, time.Unix(1500000000, 0))
	data, sum := buildTar(t, root, Options{}) // PreserveExec=false

	hdrs := readTarEntries(t, data)
	if len(hdrs) == 0 {
		t.Fatal("空归档")
	}
	// 排序检查比较逻辑路径（目录头在序列化时带尾随 /，先去掉再比）。
	logical := make([]string, len(hdrs))
	for i, h := range hdrs {
		logical[i] = strings.TrimSuffix(h.Name, "/")
	}
	for i := 1; i < len(logical); i++ {
		if logical[i-1] >= logical[i] {
			t.Fatalf("条目未按逻辑路径严格排序: %q 在 %q 之后", logical[i-1], logical[i])
		}
	}

	for _, h := range hdrs {
		if !h.ModTime.Equal(DefaultModTime) {
			t.Errorf("%s: mtime = %v, 期望 %v", h.Name, h.ModTime, DefaultModTime)
		}
		if h.Uid != 0 || h.Gid != 0 || h.Uname != "" || h.Gname != "" {
			t.Errorf("%s: 属主未清零 uid=%d gid=%d uname=%q gname=%q",
				h.Name, h.Uid, h.Gid, h.Uname, h.Gname)
		}
		switch h.Typeflag {
		case tar.TypeDir:
			if fs.FileMode(h.Mode).Perm() != ModeDir {
				t.Errorf("%s: 目录权限 %o", h.Name, h.Mode)
			}
		case tar.TypeReg:
			if fs.FileMode(h.Mode).Perm() != ModeFile {
				t.Errorf("%s: 文件权限 %o, 期望 %o", h.Name, h.Mode, ModeFile)
			}
		case tar.TypeSymlink:
			if fs.FileMode(h.Mode).Perm() != ModeSymlink {
				t.Errorf("%s: 符号链接权限 %o", h.Name, h.Mode)
			}
		}
	}

	// PreserveExec=true 时脚本应为 0755。
	dataExec, _ := buildTar(t, root, Options{PreserveExec: true})
	found := false
	for _, h := range readTarEntries(t, dataExec) {
		if h.Name == "脚本.sh" {
			found = true
			if fs.FileMode(h.Mode).Perm() != ModeFileExec {
				t.Fatalf("可执行文件权限 %o, 期望 %o", h.Mode, ModeFileExec)
			}
		}
		if h.Name == "a.txt" && fs.FileMode(h.Mode).Perm() != ModeFile {
			t.Fatalf("普通文件权限被错误保留为 %o", h.Mode)
		}
	}
	if !found {
		t.Fatal("归档中未找到 脚本.sh")
	}

	// 计数与摘要一致性。
	if sum.FileCount == 0 || sum.DirCount != 3 {
		t.Fatalf("计数异常: files=%d dirs=%d symlinks=%d",
			sum.FileCount, sum.DirCount, sum.SymlinkCount)
	}
	if sum.SymlinkCount != 4 {
		t.Fatalf("符号链接计数=%d, 期望 4", sum.SymlinkCount)
	}
	h := sha256.Sum256(data)
	if sum.ArtifactSHA256 != hex.EncodeToString(h[:]) {
		t.Fatal("摘要中的制品哈希与实际字节不匹配")
	}
	if sum.ArtifactSize != int64(len(data)) {
		t.Fatalf("制品大小 %d != 实际 %d", sum.ArtifactSize, len(data))
	}
}

// TestEmptyTree：空目录产出仅含两个零块的最小 tar，且两次构建一致。
func TestEmptyTree(t *testing.T) {
	root := t.TempDir()
	d1, s1 := buildTar(t, root, Options{})
	d2, _ := buildTar(t, root, Options{})
	if !bytes.Equal(d1, d2) {
		t.Fatal("空树归档不确定")
	}
	if len(d1) != 2*512 {
		t.Fatalf("空 tar 大小=%d, 期望 1024", len(d1))
	}
	if s1.FileCount+s1.DirCount+s1.SymlinkCount != 0 {
		t.Fatal("空树计数应为 0")
	}
}

// TestSymlinkEscapeRejected 覆盖各类必须拒绝的逃逸符号链接。
func TestSymlinkEscapeRejected(t *testing.T) {
	secret := t.TempDir()
	writeFile(t, filepath.Join(secret, "passwd"), "secret\n", 0o644, time.Unix(1, 0))

	cases := []struct {
		name   string
		target string
		setup  func(root string) // 额外布局
	}{
		{"绝对路径", secret, nil},
		{"直接父级逃逸", "../../../../../../etc/passwd", nil},
		{"嵌套相对逃逸", "../../../../../../etc/passwd", func(root string) {
			mkdir(t, filepath.Join(root, "子目录"), time.Unix(1, 0))
			symlink(t, "../../../../../../etc/passwd", filepath.Join(root, "子目录", "evil"))
		}},
		{"经目录链接跳出", "indirect", func(root string) {
			symlink(t, secret, filepath.Join(root, "escape-dir"))
			writeFile(t, filepath.Join(root, "indirect"), "", 0o644, time.Unix(1, 0))
			// 构造一个词法在树内、解析后跳出的链接：
			os.Remove(filepath.Join(root, "indirect"))
			symlink(t, filepath.Join("escape-dir", "passwd"), filepath.Join(root, "indirect"))
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			if tc.setup != nil {
				tc.setup(root)
			} else {
				symlink(t, tc.target, filepath.Join(root, "evil"))
			}
			var buf bytes.Buffer
			_, err := WriteTar(root, &buf, Options{})
			var unsafeErr *UnsafeSymlinkError
			if !errors.As(err, &unsafeErr) {
				t.Fatalf("期望 UnsafeSymlinkError, 实际: %v", err)
			}
		})
	}
}

// TestSafeSymlinksAccepted：树内相对链接、悬空链接必须被接受并正确保留。
func TestSafeSymlinksAccepted(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "data", "f.txt"), "hi\n", 0o644, time.Unix(1, 0))
	symlink(t, filepath.Join("data", "f.txt"), filepath.Join(root, "rel-link"))
	symlink(t, filepath.Join("data", "nope"), filepath.Join(root, "dangling"))
	// 两级合法链接。
	symlink(t, "rel-link", filepath.Join(root, "link-to-link"))

	data, sum := buildTar(t, root, Options{})
	links := map[string]string{}
	for _, h := range readTarEntries(t, data) {
		if h.Typeflag == tar.TypeSymlink {
			links[h.Name] = h.Linkname
		}
	}
	if links["rel-link"] != "data/f.txt" {
		t.Fatalf("rel-link 目标异常: %q", links["rel-link"])
	}
	if links["dangling"] != "data/nope" {
		t.Fatalf("dangling 目标异常: %q", links["dangling"])
	}
	if links["link-to-link"] != "rel-link" {
		t.Fatalf("link-to-link 目标异常: %q", links["link-to-link"])
	}
	if sum.SymlinkCount != 3 {
		t.Fatalf("符号链接计数=%d, 期望 3", sum.SymlinkCount)
	}
}

// TestUnsupportedTypeRejected：FIFO 等非常规类型必须报错，不得静默丢弃。
func TestUnsupportedTypeRejected(t *testing.T) {
	root := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(root, "fifo"), 0o644); err != nil {
		t.Skipf("当前环境不支持 mkfifo: %v", err)
	}
	var buf bytes.Buffer
	if _, err := WriteTar(root, &buf, Options{}); err == nil {
		t.Fatal("期望 FIFO 被拒绝，但构建成功")
	}
}

// TestCustomModTime：请求指定的固定时间应出现在每个头中，且改变它会改变字节。
func TestCustomModTime(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f"), "x\n", 0o644, time.Unix(1, 0))
	mt := time.Date(2021, 6, 15, 12, 0, 0, 0, time.UTC)
	data, _ := buildTar(t, root, Options{ModTime: mt})
	for _, h := range readTarEntries(t, data) {
		if !h.ModTime.Equal(mt) {
			t.Fatalf("%s mtime=%v, 期望 %v", h.Name, h.ModTime, mt)
		}
	}
	dataDefault, _ := buildTar(t, root, Options{})
	if bytes.Equal(data, dataDefault) {
		t.Fatal("不同固定时间不应产出相同字节")
	}
}

// TestSourceNotDirectory：源路径不是目录时返回错误。
func TestSourceNotDirectory(t *testing.T) {
	root := t.TempDir()
	f := filepath.Join(root, "file")
	writeFile(t, f, "x", 0o644, time.Unix(0, 0))
	var buf bytes.Buffer
	if _, err := WriteTar(f, &buf, Options{}); err == nil {
		t.Fatal("对文件执行打包应当失败")
	}
	if _, err := WriteTar(filepath.Join(root, "missing"), &buf, Options{}); err == nil {
		t.Fatal("对不存在的路径执行打包应当失败")
	}
}
