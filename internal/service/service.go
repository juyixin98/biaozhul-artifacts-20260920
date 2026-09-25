// Package service 编排制品差量更新的核心流程：
// 项目配方 -> 本地构建 -> 制品入库（CAS）-> 差量补丁生成 -> 原子应用。
// 纯后端、纯本地：不访问任何云服务；构建命令由调用方显式提供。
package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"deltaupdate/internal/patch"
	"deltaupdate/internal/storage"
)

// 错误码（JSON 接口的 error.code 字段）。
const (
	CodeNotFound       = "not_found"
	CodeBadRequest     = "bad_request"
	CodeDigestMismatch = "digest_mismatch"
	CodeBlockMismatch  = "block_mismatch"
	CodeCorruptPatch   = "corrupt_patch"
	CodeNoSpace        = "no_space"
	CodeBuildFailed    = "build_failed"
	CodeInternal       = "internal"
)

// Error 是带错误码的服务错误。cause 保留底层错误链，供 errors.Is/As 判定。
type Error struct {
	Code  string `json:"code"`
	Msg   string `json:"msg"`
	cause error
}

func (e *Error) Error() string { return e.Code + ": " + e.Msg }

// Unwrap 返回底层错误。
func (e *Error) Unwrap() error { return e.cause }

func newError(code, format string, args ...any) *Error {
	return &Error{Code: code, Msg: fmt.Sprintf(format, args...)}
}

// wrap 把底层错误翻译为带码错误，并保留错误链。
func wrap(err error) error {
	if err == nil {
		return nil
	}
	var se *Error
	if errors.As(err, &se) {
		return se
	}
	switch {
	case errors.Is(err, storage.ErrNotFound):
		return &Error{Code: CodeNotFound, Msg: err.Error(), cause: err}
	case errors.Is(err, storage.ErrNoSpace):
		return &Error{Code: CodeNoSpace, Msg: err.Error(), cause: err}
	case errors.Is(err, patch.ErrBadMagic), errors.Is(err, patch.ErrCorrupt):
		return &Error{Code: CodeCorruptPatch, Msg: err.Error(), cause: err}
	case errors.Is(err, patch.ErrBlockMismatch):
		return &Error{Code: CodeBlockMismatch, Msg: err.Error(), cause: err}
	default:
		return &Error{Code: CodeInternal, Msg: err.Error(), cause: err}
	}
}

// 补丁应用的阶段（崩溃注入点按此命名）。
const (
	StagePreflight = "preflight"  // 预检完成、暂存开始前
	StageStageOpen = "stage-open" // 暂存文件已创建
	StageStageCopy = "stage-copy" // 数据写入到一半
	StageVerify    = "verify"     // 摘要核验前
	StageCommit    = "commit"     // 原子切换引用前
)

// Project 是项目配方。
type Project struct {
	Name         string   `json:"name"`
	Argv         []string `json:"argv"`          // 构建命令（显式提供，不经过 shell）
	ArtifactPath string   `json:"artifact_path"` // 构建产物在工作目录内的相对路径
}

// Config 是服务配置。
type Config struct {
	CacheDir string
	StateDir string
	WorkDir  string
	// FreeSpaceCap 模拟磁盘可用空间上限（字节），0 表示不限制；用于测试空间不足。
	FreeSpaceCap uint64
	// BuildTimeout 构建命令超时，默认 120s。
	BuildTimeout time.Duration
}

// Service 是差量更新服务。
type Service struct {
	cfg   Config
	store *storage.Store

	// CrashStage 非空时，ApplyPatch 执行到对应阶段直接 os.Exit(2)，
	// 用于子进程崩溃恢复测试。
	CrashStage string
	// FaultHook 非空时，ApplyPatch 执行到对应阶段调用之；返回错误即中止
	// （进程内故障注入，用于单元测试）。
	FaultHook func(stage string) error
}

// New 创建服务并做启动恢复：清理工作目录中的中断残留。
// 缓存中的 CAS 对象与状态目录中的引用不受影响，旧制品保持可用。
func New(cfg Config) (*Service, error) {
	if cfg.BuildTimeout <= 0 {
		cfg.BuildTimeout = 120 * time.Second
	}
	st, err := storage.New(cfg.CacheDir, cfg.StateDir, cfg.WorkDir)
	if err != nil {
		return nil, err
	}
	s := &Service{cfg: cfg, store: st}
	if err := st.CleanWork(); err != nil {
		return nil, fmt.Errorf("启动恢复清理工作目录失败: %w", err)
	}
	return s, nil
}

// Store 暴露底层存储（供 HTTP 层取对象）。
func (s *Service) Store() *storage.Store { return s.store }

func (s *Service) fault(stage string) error {
	if s.FaultHook != nil {
		return s.FaultHook(stage)
	}
	if s.CrashStage != "" && s.CrashStage == stage {
		fmt.Fprintf(os.Stderr, "crash injected at stage %s\n", stage)
		os.Exit(2)
	}
	return nil
}

// ---- 项目配方 ----

func (s *Service) projectPath(name string) string {
	return filepath.Join(s.cfg.StateDir, "projects", name+".json")
}

// RegisterProject 保存/覆盖项目配方。
func (s *Service) RegisterProject(p Project) error {
	if p.Name == "" || len(p.Argv) == 0 || p.ArtifactPath == "" {
		return newError(CodeBadRequest, "name/argv/artifact_path 均不能为空")
	}
	if filepath.IsAbs(p.ArtifactPath) || filepath.Clean(p.ArtifactPath) != p.ArtifactPath ||
		p.ArtifactPath == ".." || len(p.ArtifactPath) >= 2 && p.ArtifactPath[:2] == ".." {
		return newError(CodeBadRequest, "artifact_path 必须是工作目录内的相对路径")
	}
	b, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return wrap(err)
	}
	tmp, err := os.CreateTemp(filepath.Join(s.cfg.StateDir, "projects"), "proj-*")
	if err != nil {
		return wrap(err)
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		return wrap(err)
	}
	if err := tmp.Close(); err != nil {
		return wrap(err)
	}
	if err := os.Rename(tmp.Name(), s.projectPath(p.Name)); err != nil {
		return wrap(err)
	}
	return nil
}

// GetProject 读取项目配方。
func (s *Service) GetProject(name string) (*Project, error) {
	b, err := os.ReadFile(s.projectPath(name))
	if errors.Is(err, os.ErrNotExist) {
		return nil, newError(CodeNotFound, "项目不存在: %s", name)
	}
	if err != nil {
		return nil, wrap(err)
	}
	p := &Project{}
	if err := json.Unmarshal(b, p); err != nil {
		return nil, wrap(err)
	}
	return p, nil
}

// ListProjects 返回全部项目配方。
func (s *Service) ListProjects() ([]Project, error) {
	entries, err := os.ReadDir(filepath.Join(s.cfg.StateDir, "projects"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, wrap(err)
	}
	var out []Project
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".json" {
			continue
		}
		b, err := os.ReadFile(filepath.Join(s.cfg.StateDir, "projects", e.Name()))
		if err != nil {
			continue
		}
		var p Project
		if json.Unmarshal(b, &p) == nil {
			out = append(out, p)
		}
	}
	return out, nil
}

// ---- 构建与入库 ----

// BuildResult 是构建结果。
type BuildResult struct {
	Project string `json:"project"`
	Digest  string `json:"digest"`
	Size    int64  `json:"size"`
	Output  string `json:"output"` // 构建命令的 stdout+stderr（截断）
}

// Build 在工作目录中运行项目配方的构建命令，把产物入库为当前制品。
// argv 为 nil 时使用项目配方中的命令；否则用显式给出的 argv 覆盖。
func (s *Service) Build(name string, argv []string) (*BuildResult, error) {
	p, err := s.GetProject(name)
	if err != nil {
		return nil, err
	}
	if len(argv) > 0 {
		p.Argv = argv
	}
	work, err := s.store.WorkPath("build-" + name)
	if err != nil {
		return nil, wrap(err)
	}
	defer s.store.RemoveWork(work)

	ctx, cancel := contextWithTimeout(s.cfg.BuildTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, p.Argv[0], p.Argv[1:]...)
	cmd.Dir = work
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		return nil, newError(CodeBuildFailed, "构建命令失败: %v\n%s", err, tail(out.String(), 4096))
	}
	art := filepath.Join(work, p.ArtifactPath)
	f, err := os.Open(art)
	if err != nil {
		return nil, newError(CodeBuildFailed, "构建产物不存在: %s", p.ArtifactPath)
	}
	defer f.Close()
	dgst, size, err := s.putBlobChecked(f)
	if err != nil {
		return nil, err
	}
	if err := s.store.SetRef(name, dgst); err != nil {
		return nil, wrap(err)
	}
	return &BuildResult{Project: name, Digest: dgst, Size: size, Output: tail(out.String(), 4096)}, nil
}

// Ingest 把调用方上传的原始制品字节流直接入库，并设为 name 的当前制品。
func (s *Service) Ingest(name string, r io.Reader) (*BuildResult, error) {
	if name == "" {
		return nil, newError(CodeBadRequest, "name 不能为空")
	}
	dgst, size, err := s.putBlobChecked(r)
	if err != nil {
		return nil, err
	}
	if err := s.store.SetRef(name, dgst); err != nil {
		return nil, wrap(err)
	}
	return &BuildResult{Project: name, Digest: dgst, Size: size}, nil
}

// putBlobChecked 入库前做磁盘空间预检（需要知道大小，先落临时文件）。
func (s *Service) putBlobChecked(r io.Reader) (string, int64, error) {
	tmp, err := os.CreateTemp(s.cfg.WorkDir, "ingest-*")
	if err != nil {
		return "", 0, wrap(err)
	}
	defer os.Remove(tmp.Name())
	size, err := io.Copy(tmp, r)
	if err != nil {
		tmp.Close()
		return "", 0, wrap(err)
	}
	if err := storage.CheckSpace(s.cfg.CacheDir, uint64(size), s.cfg.FreeSpaceCap); err != nil {
		tmp.Close()
		return "", 0, wrap(err)
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		tmp.Close()
		return "", 0, wrap(err)
	}
	dgst, n, err := s.store.PutBlob(tmp)
	tmp.Close()
	if err != nil {
		return "", 0, wrap(err)
	}
	return dgst, n, nil
}

// ---- 差量补丁 ----

// PatchResult 描述生成的补丁。
type PatchResult struct {
	Digest       string `json:"digest"`
	Size         int64  `json:"size"`
	OldDigest    string `json:"old_digest"`
	NewDigest    string `json:"new_digest"`
	OpCount      int    `json:"op_count"`
	CopyBytes    int64  `json:"copy_bytes"`
	LiteralBytes int64  `json:"literal_bytes"`
}

// MakePatch 生成 old -> new 的差量补丁并入库。
// oldDigest/newDigest 为空时取 name 的当前制品。
func (s *Service) MakePatch(name, oldDigest, newDigest string) (*PatchResult, error) {
	if oldDigest == "" || newDigest == "" {
		cur, err := s.store.GetRef(name)
		if err != nil {
			return nil, wrap(err)
		}
		if oldDigest == "" {
			oldDigest = cur
		}
		if newDigest == "" {
			newDigest = cur
		}
	}
	oldF, oldFI, err := s.store.OpenBlob(oldDigest)
	if err != nil {
		return nil, wrap(err)
	}
	defer oldF.Close()
	newF, newFI, err := s.store.OpenBlob(newDigest)
	if err != nil {
		return nil, wrap(err)
	}
	defer newF.Close()

	sig, err := patch.ComputeSignature(oldF, patch.DefaultBlockSize)
	if err != nil {
		return nil, wrap(err)
	}
	// 新制品整体读入内存做窗口滑动（取舍见 README）。
	target, err := io.ReadAll(io.LimitReader(newF, newFI.Size()))
	if err != nil {
		return nil, wrap(err)
	}
	ops := patch.Generate(sig, target)

	header := &patch.Header{
		Version:     1,
		BlockSize:   patch.DefaultBlockSize,
		OldDigest:   oldDigest,
		NewDigest:   newDigest,
		OldSize:     oldFI.Size(),
		NewSize:     newFI.Size(),
		CreatedUnix: time.Now().Unix(),
	}
	// 边编码边入库。
	pr, pw := io.Pipe()
	encErr := make(chan error, 1)
	go func() {
		encErr <- patch.Encode(pw, header, sig, ops)
		pw.Close()
	}()
	pdgst, psize, err := s.store.PutPatch(pr)
	if err != nil {
		return nil, wrap(err)
	}
	if err := <-encErr; err != nil {
		return nil, wrap(err)
	}

	res := &PatchResult{Digest: pdgst, Size: psize, OldDigest: oldDigest, NewDigest: newDigest, OpCount: len(ops)}
	for _, op := range ops {
		if op.Copy {
			res.CopyBytes += int64(op.BlockLen)
		} else {
			res.LiteralBytes += int64(len(op.Data))
		}
	}
	return res, nil
}

// ---- 补丁应用 ----

// ApplyResult 是应用结果。
type ApplyResult struct {
	Name      string `json:"name"`
	OldDigest string `json:"old_digest"`
	NewDigest string `json:"new_digest"`
	Size      int64  `json:"size"`
}

// ApplyPatch 把补丁应用到 name 的当前制品上，原子切换为新制品。
//
// 阶段：preflight -> stage-open -> stage-copy -> verify -> commit。
// 任一阶段失败或进程崩溃：当前引用仍指向旧制品，旧制品保持可用；
// 暂存残留由下次启动的 CleanWork 清理。重试是幂等的。
func (s *Service) ApplyPatch(name, patchDigest string) (*ApplyResult, error) {
	pf, _, err := s.store.OpenPatch(patchDigest)
	if err != nil {
		return nil, wrap(err)
	}
	defer pf.Close()
	pr, err := patch.NewReader(pf)
	if err != nil {
		return nil, wrap(err)
	}
	h := pr.H

	// --- 阶段 preflight：基线校验 + 空间预检 ---
	oldDigest, err := s.store.GetRef(name)
	if err != nil {
		return nil, wrap(err)
	}
	if oldDigest != h.OldDigest {
		return nil, newError(CodeDigestMismatch,
			"基线不匹配: 补丁期望旧制品 %s，当前为 %s", h.OldDigest, oldDigest)
	}
	oldF, _, err := s.store.OpenBlob(oldDigest)
	if err != nil {
		return nil, wrap(err)
	}
	defer oldF.Close()
	if err := storage.CheckSpace(s.cfg.WorkDir, uint64(h.NewSize), s.cfg.FreeSpaceCap); err != nil {
		return nil, wrap(err)
	}
	if err := s.fault(StagePreflight); err != nil {
		return nil, err
	}

	// --- 阶段 stage-open：在工作目录创建暂存文件 ---
	work, err := s.store.WorkPath("apply-" + name)
	if err != nil {
		return nil, wrap(err)
	}
	// 成功路径由 commit 后清理；失败/崩溃路径由 defer 或下次启动清理。
	committed := false
	defer func() {
		if !committed {
			s.store.RemoveWork(work)
		}
	}()
	stagePath := filepath.Join(work, "staged")
	stageF, err := os.OpenFile(stagePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, wrap(err)
	}
	if err := s.fault(StageStageOpen); err != nil {
		stageF.Close()
		return nil, err
	}

	// --- 阶段 stage-copy：流式重建新制品，同时累计摘要 ---
	hw := newHashWriter(stageF)
	guard := &faultWriter{w: hw, fire: func() error { return s.fault(StageStageCopy) }}
	if err := patch.Apply(pr, oldF, guard); err != nil {
		stageF.Close()
		return nil, wrap(err)
	}
	if err := stageF.Sync(); err != nil {
		stageF.Close()
		return nil, wrap(err)
	}
	if err := stageF.Close(); err != nil {
		return nil, wrap(err)
	}

	// --- 阶段 verify：最终摘要必须等于头中声明的新制品摘要 ---
	if err := s.fault(StageVerify); err != nil {
		return nil, err
	}
	if got := hw.Sum(); got != h.NewDigest {
		return nil, newError(CodeDigestMismatch,
			"重建结果摘要不匹配: 期望 %s，实际 %s（补丁损坏或基线错误）", h.NewDigest, got)
	}

	// --- 阶段 commit：新制品入库（幂等），原子切换引用 ---
	if err := s.fault(StageCommit); err != nil {
		return nil, err
	}
	staged, err := os.Open(stagePath)
	if err != nil {
		return nil, wrap(err)
	}
	dgst, size, err := s.store.PutBlob(staged)
	staged.Close()
	if err != nil {
		return nil, wrap(err)
	}
	if dgst != h.NewDigest {
		// 理论上不会发生（verify 已核对），防御性检查。
		return nil, newError(CodeInternal, "入库摘要与核验摘要不一致")
	}
	if err := s.store.SetRef(name, dgst); err != nil {
		return nil, wrap(err)
	}
	committed = true
	s.store.RemoveWork(work)
	return &ApplyResult{Name: name, OldDigest: oldDigest, NewDigest: dgst, Size: size}, nil
}

// Current 返回 name 的当前制品摘要。
func (s *Service) Current(name string) (string, error) {
	d, err := s.store.GetRef(name)
	if err != nil {
		return "", wrap(err)
	}
	return d, nil
}

// ---- 小工具 ----

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "..." + s[len(s)-n:]
}

// faultWriter 在首次写入前触发一次故障注入（模拟写了一半崩溃）。
type faultWriter struct {
	w     io.Writer
	fire  func() error
	fired bool
}

func (f *faultWriter) Write(p []byte) (int, error) {
	if !f.fired {
		f.fired = true
		if err := f.fire(); err != nil {
			return 0, err
		}
	}
	return f.w.Write(p)
}
