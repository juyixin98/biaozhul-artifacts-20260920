// Package model 定义镜像离线准入服务的领域类型与线协议（wire protocol）。
//
// 所有跨进程交换的数据（镜像配置、SBOM、签名、验签结果、豁免书、准入报告）
// 均使用 JSON；需要参与密码学摘要/签名的载荷使用 canonical 形式
// （见 cryptox.CanonicalJSON）。
package model

// ContainerConfig 是镜像运行配置中与安全策略相关的子集。
//
// 指针字段刻意保留“字段缺失”与“显式零值”的区别：
//   - user 缺失（恶意/不全的配置）与 user:""（显式空用户，运行时默认 root）
//     在策略中分别判 UNKNOWN 与 DENY，绝不能把缺失当默认值放行。
type ContainerConfig struct {
	User       *string  `json:"user,omitempty"`
	Privileged *bool    `json:"privileged,omitempty"`
	CapAdd     []string `json:"capAdd,omitempty"`
}

// BaseImageRef 指向基础镜像。允许列表按“仓库 + 精确摘要”匹配。
type BaseImageRef struct {
	Repository string `json:"repository"`
	Digest     string `json:"digest"`
}

// Image 是准入请求中的镜像配置（对镜像仓库 config blob 的离线裁剪）。
type Image struct {
	Repository string          `json:"repository"`
	Tag        string          `json:"tag,omitempty"`
	Config     ContainerConfig `json:"config"`
	BaseImage  *BaseImageRef   `json:"baseImage,omitempty"`
}

// ImageSignature 模拟镜像随附的签名产物（tag 漂移测试中它可能指向旧摘要）。
// 签名内容为镜像配置 canonical JSON 的 SHA-256 摘要。
type ImageSignature struct {
	Algorithm   string `json:"algorithm"` // 固定 ed25519
	KeyID       string `json:"keyId"`     // 签名公钥的指纹
	ImageDigest string `json:"imageDigest"`
	Signature   string `json:"signature"` // base64(ed25519 签名)
}

// VerificationResult 是本地测试验签器的结论载荷（被验签器私钥签名）。
// 它同时绑定镜像摘要与 SBOM 摘要，防止张冠李戴。
type VerificationResult struct {
	ImageDigest    string `json:"imageDigest"`
	SBOMDigest     string `json:"sbomDigest"`
	SignedBy       string `json:"signedBy"` // 镜像签名者公钥指纹
	SignatureValid bool   `json:"signatureValid"`
	VerifiedAt     string `json:"verifiedAt"` // RFC3339
	VerifierID     string `json:"verifierId"` // 验签器自身标识
}

// Envelope 是通用签名信封：验签结果与豁免书都使用该结构。
type Envelope struct {
	Payload   []byte `json:"payload"`   // canonical JSON 载荷（原样字节，验签时重新规范化）
	Algorithm string `json:"algorithm"` // ed25519
	KeyID     string `json:"keyId"`     // 签发者公钥指纹
	Signature string `json:"signature"` // base64
}

// ExemptionGrant 是豁免书载荷：绑定镜像摘要、具体规则与到期时间。
type ExemptionGrant struct {
	ID          string `json:"id"`
	ImageDigest string `json:"imageDigest"`
	RuleID      string `json:"ruleId"`    // 仅允许豁免具体规则，如 IMG-RUN-ROOT
	ExpiresAt   string `json:"expiresAt"` // RFC3339；now <= ExpiresAt 有效
	Note        string `json:"note,omitempty"`
	CreatedAt   string `json:"createdAt"`
}

// 判定状态与规则标识常量。
const (
	StatusAllow   = "ALLOW"
	StatusDeny    = "DENY"
	StatusUnknown = "UNKNOWN"
	StatusExempt  = "EXEMPT"
)

const (
	RuleRoot          = "IMG-RUN-ROOT"
	RulePrivileged    = "IMG-PRIVILEGED"
	RuleBaseAllowlist = "IMG-BASE-ALLOWLIST"
	RuleSBOM          = "IMG-SBOM"
	RuleSigned        = "IMG-SIGNED"
	RuleDigestBind    = "IMG-DIGEST-BIND"
	RuleExemptionBad  = "IMG-EXEMPTION-INVALID"
)

// AdmissionRequest 是 POST /v1/admission/evaluate 的请求体。
//
// image 为必填；sbom / verification / exemptions 使用 RawMessage，
// 以便区分“缺证据”与“空证据”，缺证据按 UNKNOWN/DENY 处理，不默认放行。
type AdmissionRequest struct {
	Image         []byte `json:"image"`
	SBOM          []byte `json:"sbom,omitempty"`
	Verification  []byte `json:"verification,omitempty"`
	Exemptions    []byte `json:"exemptions,omitempty"`
	ClaimedDigest string `json:"claimedDigest,omitempty"`
}

// ExemptionView 是豁免书验签后的服务端视图，送入 OPA 做范围判定。
type ExemptionView struct {
	ID             string `json:"id"`
	Digest         string `json:"digest"`
	RuleID         string `json:"ruleId"`
	ExpiresAt      string `json:"expiresAt"`
	ExpiresNs      int64  `json:"expiresNs"` // 由服务端按 RFC3339 解析，非法时间为 -1
	ValidSignature bool   `json:"validSignature"`
	InvalidReason  string `json:"invalidReason,omitempty"`
}

// Finding 是单条规则的逐条判定理由。
type Finding struct {
	RuleID      string `json:"ruleId"`
	Title       string `json:"title"`
	Status      string `json:"status"`
	Reason      string `json:"reason"`
	ExemptionID string `json:"exemptionId,omitempty"`
}

// Report 是一次准入评估的不可变报告。重评估生成新 ID，永不覆盖旧报告。
type Report struct {
	ID            string          `json:"id"`
	CreatedAt     string          `json:"createdAt"`
	PolicyVersion string          `json:"policyVersion"`
	PolicyHash    string          `json:"policyHash"`
	Repository    string          `json:"repository"`
	Tag           string          `json:"tag,omitempty"`
	ImageDigest   string          `json:"imageDigest"`
	RequestDigest string          `json:"requestDigest"`
	Decision      string          `json:"decision"`
	Findings      []Finding       `json:"findings"`
	Exemptions    []ExemptionView `json:"exemptionsSeen"`
}

// PolicyInfo 描述冻结的策略版本。
type PolicyInfo struct {
	Version   string           `json:"version"`
	Hash      string           `json:"hash"`
	Allowlist []AllowlistEntry `json:"allowlist"`
}

// AllowlistEntry 是基础镜像允许列表中的一条（仓库 + 摘要精确匹配）。
type AllowlistEntry struct {
	Repository string `json:"repository"`
	Digest     string `json:"digest"`
}
