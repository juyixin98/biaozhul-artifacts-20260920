package service

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	"deltaupdate/internal/patch"
)

func newTestService(t *testing.T, spaceCap uint64) (*Service, string) {
	t.Helper()
	root := t.TempDir()
	svc, err := New(Config{
		CacheDir:     filepath.Join(root, "cache"),
		StateDir:     filepath.Join(root, "state"),
		WorkDir:      filepath.Join(root, "work"),
		FreeSpaceCap: spaceCap,
		BuildTimeout: 30 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	return svc, root
}

// ingestBytes 直接入库一段制品并设为当前。
func ingestBytes(t *testing.T, svc *Service, name string, b []byte) string {
	t.Helper()
	res, err := svc.Ingest(name, bytes.NewReader(b))
	if err != nil {
		t.Fatalf("Ingest: %v", err)
	}
	return res.Digest
}

func makePattern(seed byte, n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = seed + byte(i%7)
	}
	return b
}

// buildPatchBytes 用 patch 包直接构造补丁字节（测试辅助）。
func buildPatchBytes(t *testing.T, old, target []byte, oldD, newD string) []byte {
	t.Helper()
	sig, err := patch.ComputeSignature(bytes.NewReader(old), patch.DefaultBlockSize)
	if err != nil {
		t.Fatal(err)
	}
	ops := patch.Generate(sig, target)
	h := &patch.Header{
		Version: 1, BlockSize: patch.DefaultBlockSize,
		OldDigest: oldD, NewDigest: newD,
		OldSize: int64(len(old)), NewSize: int64(len(target)),
	}
	var buf bytes.Buffer
	if err := patch.Encode(&buf, h, sig, ops); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestBuildAndHappyPath(t *testing.T) {
	svc, root := newTestService(t, 0)

	// 显式提供测试夹具命令：cp <夹具文件> artifact.bin
	fixture := filepath.Join(root, "fixture-v2.bin")
	if err := os.WriteFile(fixture, makePattern(2, 100_000), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := svc.RegisterProject(Project{
		Name:         "app",
		Argv:         []string{"cp", fixture, "artifact.bin"},
		ArtifactPath: "artifact.bin",
	}); err != nil {
		t.Fatal(err)
	}

	// v1 制品通过直接入库建立基线
	v1 := makePattern(1, 200_000)
	oldDigest := ingestBytes(t, svc, "app", v1)

	// 构建 v2
	res, err := svc.Build("app", nil)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	newDigest := res.Digest

	cur, err := svc.Current("app")
	if err != nil || cur != newDigest {
		t.Fatalf("当前制品未切换: %v %s", err, cur)
	}

	// 从 v1 -> v2 生成差量
	pr, err := svc.MakePatch("app", oldDigest, newDigest)
	if err != nil {
		t.Fatalf("MakePatch: %v", err)
	}
	// v1(200k, 模式1) 与 v2(100k, 模式2) 内容完全不同，应全部为 LITERAL
	if pr.CopyBytes != 0 {
		t.Fatalf("内容完全不同却产生了 COPY: copy=%d lit=%d", pr.CopyBytes, pr.LiteralBytes)
	}

	// 另一台“设备”：只有 v1，应用补丁后必须得到 v2 的摘要
	device, _ := newTestService(t, 0)
	ingestBytes(t, device, "app", v1)
	// 设备侧需先拿到补丁（模拟下发：从构建侧 CAS 复制到设备 CAS）
	pf, _, err := svc.Store().OpenPatch(pr.Digest)
	if err != nil {
		t.Fatal(err)
	}
	patchBytes, err := io.ReadAll(pf)
	pf.Close()
	if err != nil {
		t.Fatal(err)
	}
	devPatchDigest, _, err := device.Store().PutPatch(bytes.NewReader(patchBytes))
	if err != nil {
		t.Fatal(err)
	}
	if devPatchDigest != pr.Digest {
		t.Fatalf("补丁内容寻址不一致")
	}

	ar, err := device.ApplyPatch("app", pr.Digest)
	if err != nil {
		t.Fatalf("ApplyPatch: %v", err)
	}
	if ar.NewDigest != newDigest {
		t.Fatalf("设备最终摘要 %s != 期望 %s", ar.NewDigest, newDigest)
	}
	got, _ := device.Current("app")
	if got != newDigest {
		t.Fatalf("设备当前摘要未更新")
	}
}

func TestWrongBaselineDigest(t *testing.T) {
	svc, _ := newTestService(t, 0)
	v1 := makePattern(1, 300_000)
	v2 := makePattern(2, 300_000)
	v3 := makePattern(3, 300_000)
	d1 := ingestBytes(t, svc, "app", v1)
	d2 := ingestBytes(t, svc, "app", v2)
	d3 := ingestBytes(t, svc, "app", v3)

	pr, err := svc.MakePatch("app", d1, d2)
	if err != nil {
		t.Fatal(err)
	}
	// 当前指向 d3，与补丁头中的 OldDigest(d1) 不符 -> 错基线
	if err := svc.Store().SetRef("app", d3); err != nil {
		t.Fatal(err)
	}
	_, err = svc.ApplyPatch("app", pr.Digest)
	var se *Error
	if !errors.As(err, &se) || se.Code != CodeDigestMismatch {
		t.Fatalf("错基线应报 digest_mismatch，实际: %v", err)
	}
	cur, _ := svc.Current("app")
	if cur != d3 {
		t.Fatalf("失败后当前制品被改变: %s", cur)
	}
}

func TestCorruptPatchHeaderAndBody(t *testing.T) {
	svc, _ := newTestService(t, 0)
	v1 := makePattern(1, 300_000)
	v2 := makePattern(2, 300_000)
	d1 := ingestBytes(t, svc, "app", v1)
	d2 := ingestBytes(t, svc, "app", v2)
	pr, err := svc.MakePatch("app", d1, d2)
	if err != nil {
		t.Fatal(err)
	}

	pf, _, err := svc.Store().OpenPatch(pr.Digest)
	if err != nil {
		t.Fatal(err)
	}
	orig, err := io.ReadAll(pf)
	pf.Close()
	if err != nil {
		t.Fatal(err)
	}

	// 损坏 1：改头（头 JSON 区域内翻转一个字节）
	c1 := append([]byte{}, orig...)
	c1[30] ^= 0x01
	damagedHeader, _, err := svc.Store().PutPatch(bytes.NewReader(c1))
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.Store().SetRef("app", d1); err != nil {
		t.Fatal(err)
	}
	_, err = svc.ApplyPatch("app", damagedHeader)
	var se *Error
	if err == nil {
		t.Fatalf("损坏头的补丁必须被拒绝")
	}
	if errors.As(err, &se) && se.Code != CodeCorruptPatch && se.Code != CodeDigestMismatch {
		t.Fatalf("损坏头应报 corrupt_patch/digest_mismatch，实际 %v", err)
	}

	// 损坏 2：改体（翻转靠后字节）——强校验或最终摘要必须失败
	c2 := append([]byte{}, orig...)
	c2[len(c2)-200] ^= 0xff
	damagedBody, _, err := svc.Store().PutPatch(bytes.NewReader(c2))
	if err != nil {
		t.Fatal(err)
	}
	_, err = svc.ApplyPatch("app", damagedBody)
	if err == nil {
		t.Fatalf("损坏体的补丁必须被拒绝")
	}
	if errors.As(err, &se) {
		switch se.Code {
		case CodeCorruptPatch, CodeDigestMismatch, CodeBlockMismatch:
		default:
			t.Fatalf("损坏体错误码异常: %v", err)
		}
	}
	cur, _ := svc.Current("app")
	if cur != d1 {
		t.Fatalf("损坏补丁失败后旧制品不应被替换: %s", cur)
	}
}

func TestNoSpace(t *testing.T) {
	// 空间上限极小：应用阶段的暂存预检必须拒绝
	svc, _ := newTestService(t, 100)
	v1 := makePattern(1, 10_000)
	v2 := makePattern(2, 10_000)
	// 直接写 CAS 绕过入库预检（模拟补丁与基线已提前下发到设备）
	d1, _, err := svc.Store().PutBlob(bytes.NewReader(v1))
	if err != nil {
		t.Fatal(err)
	}
	d2, _, err := svc.Store().PutBlob(bytes.NewReader(v2))
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.Store().SetRef("app", d1); err != nil {
		t.Fatal(err)
	}
	pb := buildPatchBytes(t, v1, v2, d1, d2)
	pd, _, err := svc.Store().PutPatch(bytes.NewReader(pb))
	if err != nil {
		t.Fatal(err)
	}

	_, err = svc.ApplyPatch("app", pd)
	var se *Error
	if !errors.As(err, &se) || se.Code != CodeNoSpace {
		t.Fatalf("空间不足应报 no_space，实际: %v", err)
	}
	cur, _ := svc.Current("app")
	if cur != d1 {
		t.Fatalf("空间不足失败后旧制品被改变: %s", cur)
	}
}

func TestFaultInjectionAllStages(t *testing.T) {
	svc, _ := newTestService(t, 0)
	v1 := makePattern(1, 400_000)
	v2 := makePattern(2, 400_000)
	d1 := ingestBytes(t, svc, "app", v1)
	d2 := ingestBytes(t, svc, "app", v2)
	pr, err := svc.MakePatch("app", d1, d2)
	if err != nil {
		t.Fatal(err)
	}

	for _, stage := range []string{
		StagePreflight, StageStageOpen, StageStageCopy, StageVerify, StageCommit,
	} {
		t.Run(stage, func(t *testing.T) {
			if err := svc.Store().SetRef("app", d1); err != nil {
				t.Fatal(err)
			}
			injected := errors.New("injected failure")
			svc.FaultHook = func(s string) error {
				if s == stage {
					return injected
				}
				return nil
			}
			_, err := svc.ApplyPatch("app", pr.Digest)
			if !errors.Is(err, injected) {
				t.Fatalf("阶段 %s 未触发故障: %v", stage, err)
			}
			svc.FaultHook = nil

			// 旧制品仍可用且内容未变
			cur, _ := svc.Current("app")
			if cur != d1 {
				t.Fatalf("阶段 %s 后当前制品被改变: %s", stage, cur)
			}
			f, fi, err := svc.Store().OpenBlob(d1)
			if err != nil {
				t.Fatalf("旧制品不可读: %v", err)
			}
			if fi.Size() != int64(len(v1)) {
				t.Fatalf("旧制品大小异常: %d", fi.Size())
			}
			f.Close()
			// 工作目录不应残留暂存（进程内失败路径已清理）
			if entries, _ := os.ReadDir(svcWorkDir(svc)); len(entries) != 0 {
				t.Fatalf("阶段 %s 后工作目录残留 %d 项", stage, len(entries))
			}

			// 重试必须成功且最终摘要正确
			ar, err := svc.ApplyPatch("app", pr.Digest)
			if err != nil {
				t.Fatalf("重试失败: %v", err)
			}
			if ar.NewDigest != d2 {
				t.Fatalf("最终摘要 %s != 期望 %s", ar.NewDigest, d2)
			}
		})
	}
}

func TestIdempotentReapply(t *testing.T) {
	svc, _ := newTestService(t, 0)
	v1 := makePattern(1, 200_000)
	v2 := makePattern(2, 200_000)
	d1 := ingestBytes(t, svc, "app", v1)
	d2 := ingestBytes(t, svc, "app", v2)
	pr, err := svc.MakePatch("app", d1, d2)
	if err != nil {
		t.Fatal(err)
	}
	// 入库 v2 后当前已是 v2；先回到 v1 再应用
	if err := svc.Store().SetRef("app", d1); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.ApplyPatch("app", pr.Digest); err != nil {
		t.Fatal(err)
	}
	// 当前已是 v2，再应用同一补丁 -> 基线不匹配（明确拒绝而非损坏数据）
	_, err = svc.ApplyPatch("app", pr.Digest)
	var se *Error
	if !errors.As(err, &se) || se.Code != CodeDigestMismatch {
		t.Fatalf("对已更新制品重复应用应报 digest_mismatch，实际: %v", err)
	}
}

// svcWorkDir 取服务的工作目录（测试用）。
func svcWorkDir(s *Service) string { return s.cfg.WorkDir }
