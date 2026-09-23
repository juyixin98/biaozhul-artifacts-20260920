package core

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"
)

// ErrNoActiveKey 表示签发者没有当前有效的密钥。
var ErrNoActiveKey = errors.New("no active key for issuer")

// Store 是 Service 依赖的持久化接口（由 store 包实现）。
type Store interface {
	InsertKey(ctx context.Context, k *Key) error
	CloseActiveKeys(ctx context.Context, issuer string, at time.Time) error
	GetKey(ctx context.Context, kid string) (*Key, error)
	ActiveKey(ctx context.Context, issuer string) (*Key, error)
	ListKeys(ctx context.Context, issuer string) ([]Key, error)
	InsertCredential(ctx context.Context, c *Credential) error
	GetCredential(ctx context.Context, id string) (*Credential, error)
	InsertRevocation(ctx context.Context, credentialID, reason string) (*RevocationEvent, error)
	ListRevocations(ctx context.Context, credentialID string) ([]RevocationEvent, error)
	IsRevokedAt(ctx context.Context, credentialID string, at time.Time, maxSeq int64) (bool, error)
	Snapshot(ctx context.Context) (int64, error)
}

// IssueInput 是发行一张凭证的输入。
type IssueInput struct {
	Issuer    string
	Subject   string
	Purpose   string
	NotBefore time.Time
	NotAfter  time.Time
	Content   string // 明文内容；只保存其 sha256 摘要
}

// Service 是发行/撤销/验证的领域服务。
type Service struct {
	st Store

	// 验证结果缓存。键中包含快照号：任何撤销都会推进全局快照号，
	// 使撤销前缓存的结论永久不可命中，因此缓存绝不会在撤销后给出旧结论。
	mu    sync.Mutex
	cache map[string]VerifyResult
}

// NewService 创建领域服务。
func NewService(st Store) *Service {
	return &Service{st: st, cache: make(map[string]VerifyResult)}
}

// newID 生成带前缀的随机 id（合成标识，非真实身份）。
func newID(prefix string) (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return prefix + hex.EncodeToString(b[:]), nil
}

// CreateKey 为签发者生成一把新的 ed25519 测试密钥，自 validFrom 起生效。
// 若该签发者已有有效密钥，则把旧密钥的生效区间封闭到 validFrom —— 即密钥轮换。
// 旧密钥及其历史区间保留，用于验证轮换前签发的凭证。
func (s *Service) CreateKey(ctx context.Context, issuer string, validFrom time.Time) (*Key, error) {
	pub, sec, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	kid, err := newID("key_")
	if err != nil {
		return nil, err
	}
	k := &Key{
		Kid:       kid,
		Issuer:    issuer,
		PublicKey: pub,
		SecretKey: sec,
		ValidFrom: validFrom.UTC(),
	}
	if err := s.st.CloseActiveKeys(ctx, issuer, validFrom.UTC()); err != nil {
		return nil, err
	}
	if err := s.st.InsertKey(ctx, k); err != nil {
		return nil, err
	}
	return k, nil
}

// ListKeys 列出一个签发者的全部密钥及其生效区间。
func (s *Service) ListKeys(ctx context.Context, issuer string) ([]Key, error) {
	return s.st.ListKeys(ctx, issuer)
}

// Issue 用签发者当前有效的密钥发行一张签名凭证，返回凭证与当前快照号。
func (s *Service) Issue(ctx context.Context, in IssueInput) (*Credential, int64, error) {
	if !in.NotBefore.Before(in.NotAfter) {
		return nil, 0, fmt.Errorf("not_before must be before not_after")
	}
	k, err := s.st.ActiveKey(ctx, in.Issuer)
	if errors.Is(err, ErrNotFound) {
		return nil, 0, ErrNoActiveKey
	}
	if err != nil {
		return nil, 0, err
	}
	id, err := newID("cred_")
	if err != nil {
		return nil, 0, err
	}
	c := &Credential{
		ID:            id,
		Issuer:        in.Issuer,
		Subject:       in.Subject,
		Purpose:       in.Purpose,
		NotBefore:     in.NotBefore.UTC(),
		NotAfter:      in.NotAfter.UTC(),
		ContentDigest: DigestContent(in.Content),
		Kid:           k.Kid,
	}
	c.Signature = Sign(k, c)
	if err := s.st.InsertCredential(ctx, c); err != nil {
		return nil, 0, err
	}
	snap, err := s.st.Snapshot(ctx)
	if err != nil {
		return nil, 0, err
	}
	return c, snap, nil
}

// GetCredential 读取一张凭证。
func (s *Service) GetCredential(ctx context.Context, id string) (*Credential, error) {
	return s.st.GetCredential(ctx, id)
}

// Revoke 追加一条撤销事件（只追加，不修改任何历史行），
// 返回该事件及其快照号 —— 与验证接口返回的快照号属同一序列。
func (s *Service) Revoke(ctx context.Context, credentialID, reason string) (*RevocationEvent, error) {
	if _, err := s.st.GetCredential(ctx, credentialID); err != nil {
		return nil, err
	}
	return s.st.InsertRevocation(ctx, credentialID, reason)
}

// ListRevocations 列出一凭证的全部撤销事件。
func (s *Service) ListRevocations(ctx context.Context, credentialID string) ([]RevocationEvent, error) {
	return s.st.ListRevocations(ctx, credentialID)
}

// Snapshot 返回当前全局快照号。
func (s *Service) Snapshot(ctx context.Context) (int64, error) {
	return s.st.Snapshot(ctx)
}

// Verify 按请求时刻 At 重放撤销历史并给出结论。
//
// 流程：先取当前快照号 snap，再据 (凭证, 用途, 时刻, 内容摘要, snap) 查缓存；
// 未命中时重放“recorded_at <= At 且 seq <= snap”的撤销事件 —— 结论被钉在
// 快照 snap 上，与并发进行的撤销互不影响。任何后续撤销都会推进快照号，
// 使本缓存条目不再被命中。
func (s *Service) Verify(ctx context.Context, req VerifyRequest) (VerifyResult, error) {
	at := req.At
	if at.IsZero() {
		at = time.Now()
	}
	at = at.UTC()

	snap, err := s.st.Snapshot(ctx)
	if err != nil {
		return VerifyResult{}, err
	}

	contentDigest := ""
	if req.Content != nil {
		contentDigest = DigestContent(*req.Content)
	}
	cacheKey := fmt.Sprintf("%s|%s|%d|%s|%d",
		req.CredentialID, req.Purpose, at.UnixNano(), contentDigest, snap)

	s.mu.Lock()
	if hit, ok := s.cache[cacheKey]; ok {
		s.mu.Unlock()
		hit.CacheHit = true
		return hit, nil
	}
	s.mu.Unlock()

	res, err := s.verifyUncached(ctx, req, at, contentDigest, snap)
	if err != nil {
		return VerifyResult{}, err
	}

	s.mu.Lock()
	s.cache[cacheKey] = res
	s.mu.Unlock()
	return res, nil
}

// verifyUncached 实际执行密码学校验与历史重放。
func (s *Service) verifyUncached(ctx context.Context, req VerifyRequest, at time.Time, contentDigest string, snap int64) (VerifyResult, error) {
	c, err := s.st.GetCredential(ctx, req.CredentialID)
	if err != nil {
		return VerifyResult{}, err
	}
	k, err := s.st.GetKey(ctx, c.Kid)
	if err != nil {
		return VerifyResult{}, err
	}

	reasons := []string{}

	// 1. 签名真实性：用凭证记载的 kid 找到对应公钥验签。
	if !VerifySignature(k.PublicKey, c, c.Signature) {
		reasons = append(reasons, ReasonSignatureInvalid)
	}
	// 2. 密钥区间：签发时刻必须落在该密钥的生效区间内（轮换前后各归各的区间）。
	if !k.Active(c.IssuedAt) {
		reasons = append(reasons, ReasonKeyInactiveAtIssuance)
	}
	// 3. 用途匹配。
	if req.Purpose != c.Purpose {
		reasons = append(reasons, ReasonPurposeMismatch)
	}
	// 4. 有效期：语义为 [not_before, not_after)，
	//    at == not_before 有效，at == not_after 已过期。
	if at.Before(c.NotBefore) {
		reasons = append(reasons, ReasonNotYetValid)
	}
	if !at.Before(c.NotAfter) {
		reasons = append(reasons, ReasonExpired)
	}
	// 5. 内容摘要（请求方提供内容时校验）。
	if req.Content != nil && contentDigest != c.ContentDigest {
		reasons = append(reasons, ReasonDigestMismatch)
	}
	// 6. 撤销重放：只统计 at 时刻之前、且不晚于快照 snap 的撤销事件，
	//    保证历史查询不被之后的撤销改写。
	revoked, err := s.st.IsRevokedAt(ctx, c.ID, at, snap)
	if err != nil {
		return VerifyResult{}, err
	}
	if revoked {
		reasons = append(reasons, ReasonRevoked)
	}

	status := StatusValid
	if len(reasons) > 0 {
		status = StatusInvalid
	}
	return VerifyResult{
		CredentialID: c.ID,
		Status:       status,
		Reasons:      reasons,
		Snapshot:     snap,
		CheckedAt:    at,
	}, nil
}
