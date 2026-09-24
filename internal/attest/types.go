package attest

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"
)

const (
	// PurposeV1 是证明的唯一合法用途标识，写进被签名内容，
	// 用于拒绝跨用途签名（例如把其它协议的签名伪装成构建证明）。
	PurposeV1 = "build-attestation/v1"

	// Algorithm 是签名对象中唯一被接受的算法标识。
	Algorithm = "ed25519"

	// DomainSep 是签名消息的前缀域分隔符：
	// 实际被签名的字节 = DomainSep || Canonical(attestation)。
	DomainSep = "ATTESTATION-V1\x00"
)

var (
	digestRe  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	commitRe  = regexp.MustCompile(`^[0-9a-f]{40}$`)
	idRe      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$`)
	builderRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/@-]{0,127}$`)
	keyIDRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
)

// Attestation 是被签名的构建证明主体。
type Attestation struct {
	AttestationID  string            // 一次性随机 ID，用于重放检测
	Purpose        string            // 必须等于 PurposeV1
	ArtifactDigest string            // "sha256:" + 64 位小写十六进制
	SourceRepo     string            // https 仓库 URL
	SourceCommit   string            // 40 位小写十六进制 git commit
	Builder        string            // builder 身份标识
	BuildParams    map[string]string // 构建参数（键值均为字符串）
	Timestamp      time.Time         // UTC，秒级精度
}

// Envelope 是线上传输的封装：证明主体 + 签名。
type Envelope struct {
	Attestation          Attestation
	AttestationCanonical []byte // 参与签名的规范字节（由服务端自行重建）
	KeyID                string
	Sig                  []byte
}

// attestationMap 把 Attestation 转成待规范化的通用对象。
func attestationMap(a Attestation) map[string]any {
	params := make(map[string]any, len(a.BuildParams))
	for k, v := range a.BuildParams {
		params[k] = v
	}
	return map[string]any{
		"attestationId":  a.AttestationID,
		"purpose":        a.Purpose,
		"artifactDigest": a.ArtifactDigest,
		"sourceRepo":     a.SourceRepo,
		"sourceCommit":   a.SourceCommit,
		"builder":        a.Builder,
		"buildParams":    params,
		"timestamp":      a.Timestamp.UTC().Format(time.RFC3339),
	}
}

// canonicalAttestation 返回证明主体的规范 JSON 字节。
func canonicalAttestation(a Attestation) ([]byte, error) {
	return Canonical(attestationMap(a))
}

// signMessage 返回实际参与 ed25519 运算的消息：域分隔前缀 + 规范 JSON。
func signMessage(canonical []byte) []byte {
	msg := make([]byte, 0, len(DomainSep)+len(canonical))
	msg = append(msg, DomainSep...)
	msg = append(msg, canonical...)
	return msg
}

// SignEnvelope 用私钥对证明签名，输出规范 JSON 封装。
func SignEnvelope(a Attestation, keyID string, priv ed25519.PrivateKey) ([]byte, error) {
	canon, err := canonicalAttestation(a)
	if err != nil {
		return nil, err
	}
	sig := ed25519.Sign(priv, signMessage(canon))
	env := map[string]any{
		"attestation": attestationMap(a),
		"signature": map[string]any{
			"keyId":     keyID,
			"algorithm": Algorithm,
			"sig":       base64.StdEncoding.EncodeToString(sig),
		},
	}
	return Canonical(env)
}

// parseEnvelope 从严格解析后的 JSON 值中提取并校验封装。
// 任何未知字段、类型错误或格式错误都会被拒绝。
func parseEnvelope(v any) (*Envelope, error) {
	root, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("封装必须是 JSON 对象")
	}
	if err := requireKeys(root, "envelope", "attestation", "signature"); err != nil {
		return nil, err
	}

	attObj, ok := root["attestation"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("attestation 必须是对象")
	}
	if err := requireKeys(attObj, "attestation",
		"attestationId", "purpose", "artifactDigest", "sourceRepo",
		"sourceCommit", "builder", "buildParams", "timestamp"); err != nil {
		return nil, err
	}

	sigObj, ok := root["signature"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("signature 必须是对象")
	}
	if err := requireKeys(sigObj, "signature", "keyId", "algorithm", "sig"); err != nil {
		return nil, err
	}

	var a Attestation
	var err error
	if a.AttestationID, err = strField(attObj, "attestationId"); err != nil {
		return nil, err
	}
	if !idRe.MatchString(a.AttestationID) {
		return nil, fmt.Errorf("attestationId 格式非法")
	}
	if a.Purpose, err = strField(attObj, "purpose"); err != nil {
		return nil, err
	}
	if a.ArtifactDigest, err = strField(attObj, "artifactDigest"); err != nil {
		return nil, err
	}
	if !digestRe.MatchString(a.ArtifactDigest) {
		return nil, fmt.Errorf("artifactDigest 必须是 sha256: 加 64 位小写十六进制")
	}
	if a.SourceRepo, err = strField(attObj, "sourceRepo"); err != nil {
		return nil, err
	}
	if err := validRepoURL(a.SourceRepo); err != nil {
		return nil, err
	}
	if a.SourceCommit, err = strField(attObj, "sourceCommit"); err != nil {
		return nil, err
	}
	if !commitRe.MatchString(a.SourceCommit) {
		return nil, fmt.Errorf("sourceCommit 必须是 40 位小写十六进制")
	}
	if a.Builder, err = strField(attObj, "builder"); err != nil {
		return nil, err
	}
	if !builderRe.MatchString(a.Builder) {
		return nil, fmt.Errorf("builder 格式非法")
	}
	tsStr, err := strField(attObj, "timestamp")
	if err != nil {
		return nil, err
	}
	// time.RFC3339 布局中的 "Z07:00" 也接受 "+08:00" 等偏移；
	// 这里额外要求字面值以 "Z" 结尾，强制规范 UTC 表示。
	if !strings.HasSuffix(tsStr, "Z") {
		return nil, fmt.Errorf("timestamp 必须是 UTC（以 Z 结尾）")
	}
	if a.Timestamp, err = time.Parse(time.RFC3339, tsStr); err != nil {
		return nil, fmt.Errorf("timestamp 不是合法的 RFC3339: %v", err)
	}
	if a.Timestamp.Nanosecond() != 0 {
		return nil, fmt.Errorf("timestamp 只允许秒级精度")
	}

	paramsObj, ok := attObj["buildParams"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("buildParams 必须是对象")
	}
	a.BuildParams = make(map[string]string, len(paramsObj))
	for k, pv := range paramsObj {
		s, ok := pv.(string)
		if !ok {
			return nil, fmt.Errorf("buildParams[%q] 必须是字符串", k)
		}
		a.BuildParams[k] = s
	}

	keyID, err := strField(sigObj, "keyId")
	if err != nil {
		return nil, err
	}
	if !keyIDRe.MatchString(keyID) {
		return nil, fmt.Errorf("keyId 格式非法")
	}
	alg, err := strField(sigObj, "algorithm")
	if err != nil {
		return nil, err
	}
	if alg != Algorithm {
		return nil, fmt.Errorf("不支持的签名算法 %q", alg)
	}
	sigB64, err := strField(sigObj, "sig")
	if err != nil {
		return nil, err
	}
	sig, err := base64.StdEncoding.DecodeString(sigB64)
	if err != nil {
		return nil, fmt.Errorf("sig 不是合法的 base64: %v", err)
	}
	if len(sig) != ed25519.SignatureSize {
		return nil, fmt.Errorf("sig 长度必须是 %d 字节", ed25519.SignatureSize)
	}

	canon, err := canonicalAttestation(a)
	if err != nil {
		return nil, err
	}
	return &Envelope{
		Attestation:          a,
		AttestationCanonical: canon,
		KeyID:                keyID,
		Sig:                  sig,
	}, nil
}

// requireKeys 要求对象恰好包含给定键：缺键或多键都报错。
func requireKeys(obj map[string]any, name string, keys ...string) error {
	want := make(map[string]bool, len(keys))
	for _, k := range keys {
		want[k] = true
	}
	for k := range obj {
		if !want[k] {
			return fmt.Errorf("%s 包含未知字段 %q", name, k)
		}
	}
	for _, k := range keys {
		if _, ok := obj[k]; !ok {
			return fmt.Errorf("%s 缺少必需字段 %q", name, k)
		}
	}
	return nil
}

func strField(obj map[string]any, key string) (string, error) {
	s, ok := obj[key].(string)
	if !ok {
		return "", fmt.Errorf("字段 %q 必须是字符串", key)
	}
	return s, nil
}

func validRepoURL(s string) error {
	u, err := url.Parse(s)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return fmt.Errorf("sourceRepo 必须是 https URL")
	}
	if u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("sourceRepo 不允许包含用户信息、查询串或片段")
	}
	return nil
}
