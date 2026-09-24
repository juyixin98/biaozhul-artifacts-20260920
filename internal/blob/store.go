// Package blob 是内容寻址的本地产物仓库。
// 物理路径: <root>/ab/<digest hex 前2字节>/<digest hex>
// 同 digest 全局只存一份；按环境“复制”时做真实字节拷贝到 <root>/envs/<env>/<digest hex>，
// 复制完成后重新流式 sha256 校验，校验不过即删除目标并报错——环境永远不会看到半份产物。
package blob

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"atomicpromo/internal/crypto/sig"
)

type Store struct {
	root string
}

func New(root string) (*Store, error) {
	for _, d := range []string{"ab", "envs", "tmp"} {
		if err := os.MkdirAll(filepath.Join(root, d), 0o755); err != nil {
			return nil, err
		}
	}
	return &Store{root: root}, nil
}

func digestHex(digest string) (string, error) {
	if !strings.HasPrefix(digest, sig.DigestPrefix) {
		return "", fmt.Errorf("digest must start with %q", sig.DigestPrefix)
	}
	hx := strings.TrimPrefix(digest, sig.DigestPrefix)
	if len(hx) != 64 {
		return "", errors.New("digest must be sha256 hex (64 chars)")
	}
	return hx, nil
}

// CanonicalPath 内容库内的规范相对路径
func CanonicalPath(digest string) (string, error) {
	hx, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return filepath.Join("ab", hx[:2], hx), nil
}

func (s *Store) EnvPath(env, digest string) (string, error) {
	hx, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return filepath.Join(s.root, "envs", env, hx), nil
}

// PutCanonical 流式写入内容库，返回重新计算出的 digest。落盘 tmp+fsync+rename，崩溃不留半成品。
// 同 digest 已存在时幂等。
func (s *Store) PutCanonical(r io.Reader) (digest string, size int64, err error) {
	tmp, err := os.CreateTemp(filepath.Join(s.root, "tmp"), "upload-*")
	if err != nil {
		return "", 0, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // rename 成功后为 no-op

	n, err := copyAndHash(tmp, r)
	if err != nil {
		tmp.Close()
		return "", 0, err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return "", 0, err
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}
	digest = n.digest

	rel, err := CanonicalPath(digest)
	if err != nil {
		return "", 0, err
	}
	dst := filepath.Join(s.root, rel)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", 0, err
	}
	if err := os.Link(tmpName, dst); err != nil {
		if errors.Is(err, os.ErrExist) {
			return digest, n.bytes, nil // 幂等
		}
		if err := os.Rename(tmpName, dst); err != nil { // 回退（跨设备）
			return "", 0, err
		}
	}
	return digest, n.bytes, nil
}

// CopyToEnv 把内容库产物真实复制到环境目录，复制完成后对新文件独立重新流式 sha256 校验。
// 校验失败（如故障注入改了字节）删除临时文件并返回 ErrVerify——指针尚未切换，旧版本不受影响。
// 目标已存在且校验通过时幂等（崩溃恢复场景）；已存在但损坏则删除后报错。
func (s *Store) CopyToEnv(ctx context.Context, env, expectedDigest string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	rel, err := CanonicalPath(expectedDigest)
	if err != nil {
		return "", err
	}
	src := filepath.Join(s.root, rel)
	dst, err := s.EnvPath(env, expectedDigest)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", err
	}

	if _, statErr := os.Stat(dst); statErr == nil {
		got, err := hashFile(dst)
		if err != nil {
			return "", err
		}
		if got != expectedDigest {
			os.Remove(dst)
			return "", fmt.Errorf("%w: existing env blob corrupt: got %s want %s", ErrVerify, got, expectedDigest)
		}
		return got, nil
	} else if !errors.Is(statErr, os.ErrNotExist) {
		return "", statErr
	}

	tmp, err := os.CreateTemp(filepath.Join(s.root, "tmp"), "copy-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	in, err := os.Open(src)
	if err != nil {
		tmp.Close()
		if errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("source artifact missing from blob store: %w", err)
		}
		return "", err
	}
	fr := newFaultReader(ctx, in, env, expectedDigest)
	_, copyErr := io.Copy(tmp, fr)
	in.Close()
	if syncErr := tmp.Sync(); copyErr == nil {
		copyErr = syncErr
	}
	tmp.Close()
	if copyErr != nil {
		return "", copyErr
	}

	// 关键步骤：切换指针前，对新文件独立重新哈希
	verified, err := hashFile(tmpName)
	if err != nil {
		return "", err
	}
	if verified != expectedDigest {
		return "", fmt.Errorf("%w: copied bytes hash %s, expected %s", ErrVerify, verified, expectedDigest)
	}

	if err := os.Rename(tmpName, dst); err != nil {
		return "", err
	}
	return verified, nil
}

// RemoveEnvBlob 删除环境副本（GC 用）
func (s *Store) RemoveEnvBlob(env, digest string) error {
	dst, err := s.EnvPath(env, digest)
	if err != nil {
		return err
	}
	if err := os.Remove(dst); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// RemoveCanonical 删除内容库原件
func (s *Store) RemoveCanonical(digest string) error {
	rel, err := CanonicalPath(digest)
	if err != nil {
		return err
	}
	if err := os.Remove(filepath.Join(s.root, rel)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func (s *Store) EnvBlobExists(env, digest string) (bool, error) {
	dst, err := s.EnvPath(env, digest)
	if err != nil {
		return false, err
	}
	_, err = os.Stat(dst)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	return sig.HashReader(f)
}

// ErrVerify 复制后内容校验失败
var ErrVerify = errors.New("blob verification failed")

type copyResult struct {
	bytes  int64
	digest string
}

func copyAndHash(w io.Writer, r io.Reader) (copyResult, error) {
	h := newSHA256()
	n, err := io.Copy(io.MultiWriter(w, h), r)
	if err != nil {
		return copyResult{}, err
	}
	return copyResult{bytes: n, digest: h.digest()}, nil
}

// ---- 故障注入：仅测试使用，通过 context 传入 ----

type faultKeyType struct{}

var faultKey faultKeyType

// Fault 描述一次复制故障：
// CorruptBytes>0 时在复制流第 CorruptAt 字节翻转一个比特，制造 digest 不匹配（复制失败）；
// CrashBeforePointerCommit=true 时在“复制校验完成、提交指针前”触发回调（模拟进程崩溃）。
type Fault struct {
	CorruptBytes             int
	CrashBeforePointerCommit bool
	OnCrash                  func() // 测试在此 kill 进程/记录状态
}

func WithFault(ctx context.Context, f *Fault) context.Context {
	return context.WithValue(ctx, faultKey, f)
}

func faultFromCtx(ctx context.Context) *Fault {
	f, _ := ctx.Value(faultKey).(*Fault)
	return f
}

// crashHook 在事务提交指针前由 service 调用
func CrashHook(ctx context.Context) {
	if f := faultFromCtx(ctx); f != nil && f.CrashBeforePointerCommit {
		if f.OnCrash != nil {
			f.OnCrash()
		}
		panic(crashSentinel{}) // 默认行为；测试可用 OnCrash 里 os.Exit 模拟真实崩溃
	}
}

type crashSentinel struct{}

func (crashSentinel) Error() string { return "simulated crash before pointer commit" }

// faultReader 在复制过程中翻转指定字节
type faultReader struct {
	ctx    context.Context
	inner  io.Reader
	env    string
	digest string
	mu     sync.Mutex
	read   int
}

func newFaultReader(ctx context.Context, r io.Reader, env, digest string) *faultReader {
	return &faultReader{ctx: ctx, inner: r, env: env, digest: digest}
}

func (fr *faultReader) Read(p []byte) (int, error) {
	n, err := fr.inner.Read(p)
	if flt := faultFromCtx(fr.ctx); flt != nil && flt.CorruptBytes > 0 && n > 0 {
		fr.mu.Lock()
		if fr.read < flt.CorruptBytes {
			idx := flt.CorruptBytes - fr.read - 1
			if idx < n {
				p[idx] ^= 0xFF
			}
		}
		fr.read += n
		fr.mu.Unlock()
	}
	return n, err
}
