package main

// 崩溃恢复测试：以子进程方式运行 crash-helper，在补丁应用的每个阶段
// 让进程崩溃退出（os.Exit(2)），验证：
//   1. 旧制品（当前引用）保持可用且内容不变；
//   2. 重启后（新进程）清理暂存残留；
//   3. 重试应用成功，最终摘要与目标一致。

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"deltaupdate/internal/digest"
	"deltaupdate/internal/service"
)

var stages = []string{
	service.StagePreflight,
	service.StageStageOpen,
	service.StageStageCopy,
	service.StageVerify,
	service.StageCommit,
}

func buildHelper(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "deltaupd-test")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	cmd.Dir = "."
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("构建测试二进制失败: %v\n%s", err, out)
	}
	return bin
}

// setupRoot 在 root 下准备 cache/state/work，并生成 v1->v2 补丁。
// 返回后 root 的当前制品为 v1，补丁已入库。
func setupRoot(t *testing.T, root string, v1, v2 []byte) (patchDigest, newDigest string) {
	t.Helper()
	svc, err := service.New(service.Config{
		CacheDir: filepath.Join(root, "cache"),
		StateDir: filepath.Join(root, "state"),
		WorkDir:  filepath.Join(root, "work"),
	})
	if err != nil {
		t.Fatal(err)
	}
	d1, err := svc.Ingest("app", bytes.NewReader(v1))
	if err != nil {
		t.Fatal(err)
	}
	d2, err := svc.Ingest("app", bytes.NewReader(v2))
	if err != nil {
		t.Fatal(err)
	}
	newDigest = d2.Digest
	pr, err := svc.MakePatch("app", d1.Digest, newDigest)
	if err != nil {
		t.Fatal(err)
	}
	// 当前制品回到 v1，等待应用补丁
	if err := svc.Store().SetRef("app", d1.Digest); err != nil {
		t.Fatal(err)
	}
	return pr.Digest, newDigest
}

func runHelper(t *testing.T, bin, root, patchDigest, crashStage string) (string, string, int) {
	t.Helper()
	cmd := exec.Command(bin, "-root", root, "-name", "app", "-patch", patchDigest)
	cmd.Env = append(os.Environ(),
		"DELTA_CRASH_HELPER=1",
		"DELTA_CRASH_STAGE="+crashStage,
	)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := 0
	if exitErr, ok := err.(*exec.ExitError); ok {
		code = exitErr.ExitCode()
	} else if err != nil {
		t.Fatalf("运行 helper 失败: %v", err)
	}
	return stdout.String(), stderr.String(), code
}

func TestCrashRecoveryAllStages(t *testing.T) {
	bin := buildHelper(t)
	v1 := make([]byte, 500_000)
	for i := range v1 {
		v1[i] = byte(i % 11)
	}
	v2 := make([]byte, 500_000)
	for i := range v2 {
		v2[i] = byte(i%11) + 3
	}
	oldDigest := digest.Bytes(v1)

	for _, stage := range stages {
		t.Run(stage, func(t *testing.T) {
			root := t.TempDir()
			patchDigest, newDigest := setupRoot(t, root, v1, v2)

			// 1. 在该阶段崩溃
			_, stderr, code := runHelper(t, bin, root, patchDigest, stage)
			if code != 2 {
				t.Fatalf("阶段 %s 应以退出码 2 崩溃，实际 %d（stderr: %s）", stage, code, stderr)
			}

			// 2. 崩溃后旧制品必须仍是当前制品且完整可读
			svc, err := service.New(service.Config{
				CacheDir: filepath.Join(root, "cache"),
				StateDir: filepath.Join(root, "state"),
				WorkDir:  filepath.Join(root, "work"),
			})
			if err != nil {
				t.Fatal(err)
			}
			cur, err := svc.Current("app")
			if err != nil {
				t.Fatal(err)
			}
			if cur != oldDigest {
				t.Fatalf("阶段 %s 崩溃后当前制品变为 %s（应为旧制品 %s）", stage, cur, oldDigest)
			}
			f, fi, err := svc.Store().OpenBlob(cur)
			if err != nil {
				t.Fatalf("旧制品不可读: %v", err)
			}
			got, err := digest.Reader(f)
			f.Close()
			if err != nil || got != oldDigest || fi.Size() != int64(len(v1)) {
				t.Fatalf("旧制品内容损坏: digest=%s size=%d err=%v", got, fi.Size(), err)
			}

			// 3. 重启清理了暂存残留
			entries, err := os.ReadDir(filepath.Join(root, "work"))
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) != 0 {
				t.Fatalf("重启后工作目录残留 %d 项", len(entries))
			}

			// 4. 重试应用成功，最终摘要正确
			stdout, stderr, code := runHelper(t, bin, root, patchDigest, "")
			if code != 0 {
				t.Fatalf("重试应用失败（退出码 %d）: %s", code, stderr)
			}
			if !bytes.Contains([]byte(stdout), []byte(newDigest)) {
				t.Fatalf("重试输出未包含新摘要: %s", stdout)
			}
			svc2, err := service.New(service.Config{
				CacheDir: filepath.Join(root, "cache"),
				StateDir: filepath.Join(root, "state"),
				WorkDir:  filepath.Join(root, "work"),
			})
			if err != nil {
				t.Fatal(err)
			}
			cur2, _ := svc2.Current("app")
			if cur2 != newDigest {
				t.Fatalf("重试后当前摘要 %s != 期望 %s", cur2, newDigest)
			}
			// 最终制品内容核验
			f2, _, err := svc2.Store().OpenBlob(cur2)
			if err != nil {
				t.Fatal(err)
			}
			got2, _ := digest.Reader(f2)
			f2.Close()
			if got2 != digest.Bytes(v2) {
				t.Fatalf("最终制品摘要 %s != v2 摘要 %s", got2, digest.Bytes(v2))
			}
		})
	}
}

// TestCrashHelperWithoutStage 无崩溃注入时 helper 直接成功。
func TestCrashHelperWithoutStage(t *testing.T) {
	bin := buildHelper(t)
	v1 := bytes.Repeat([]byte("old-"), 10000)
	v2 := bytes.Repeat([]byte("new!"), 10000)
	root := t.TempDir()
	patchDigest, newDigest := setupRoot(t, root, v1, v2)
	stdout, stderr, code := runHelper(t, bin, root, patchDigest, "")
	if code != 0 {
		t.Fatalf("正常应用失败: %d %s", code, stderr)
	}
	if !bytes.Contains([]byte(stdout), []byte(newDigest)) {
		t.Fatalf("输出缺少新摘要: %s", stdout)
	}
}
