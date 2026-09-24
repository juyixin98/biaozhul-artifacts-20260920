// Package evaluate 编排准入判定：真实的摘要/签名/验签密码学操作在 Go 中完成，
// 策略规则与豁免范围判定在冻结的 OPA Rego 策略中完成。
package evaluate

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/open-policy-agent/opa/rego"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/model"
	"mirrorsec/internal/policy"
)

// PolicyVersion 是策略的冻结版本号。修改策略必须提升版本号。
const PolicyVersion = policy.Version

// 证据状态（送入 Rego 的 input.evidence）。
const (
	evSigned                  = "SIGNED"
	evUnsigned                = "UNSIGNED"
	evBadSignature            = "BAD_SIGNATURE"
	evUntrustedKey            = "UNTRUSTED_KEY"
	evNoVerification          = "NO_VERIFICATION"
	evBadVerificationEnvelope = "BAD_VERIFICATION_ENVELOPE"
	evDigestDrift             = "DIGEST_DRIFT"
)

// Clock 允许测试注入固定时间（边界时间用例）。
type Clock interface{ Now() time.Time }

type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now().UTC() }

// Trust 是准入服务持有的受信公钥集合。
type Trust struct {
	Verifier           []byte // 验签器公钥 PEM（验证 verification 信封）
	ExemptionAuthority []byte // 豁免签发机构公钥 PEM（验证豁免书）
	TrustedSignerPEMs  [][]byte
}

// Engine 是线程安全的准入评估器；策略在构造时编译并冻结。
type Engine struct {
	prepared  rego.PreparedEvalQuery
	version   string
	hash      string
	allowlist []model.AllowlistEntry
	verifier  []byte
	exemptKey []byte
	signerIDs map[string]struct{}
	clock     Clock
}

// PolicyHash 对“策略源码 + 允许列表”做摘要，允许列表也是冻结策略的一部分。
func policyHash(entries []model.AllowlistEntry) (string, error) {
	allowCanonical, err := cryptox.CanonicalJSON(entries)
	if err != nil {
		return "", err
	}
	return cryptox.SHA256Hex([]byte(PolicyVersion + "\n" + policy.Source + "\n" + string(allowCanonical))), nil
}

// NewEngine 编译并冻结策略。allowlist 为 nil 时使用嵌入的示例允许列表。
func NewEngine(ctx context.Context, trust Trust, allowlist []model.AllowlistEntry, clock Clock) (*Engine, error) {
	if clock == nil {
		clock = RealClock{}
	}
	if len(allowlist) == 0 {
		return nil, errors.New("允许列表为空：拒绝在无允许列表的情况下启动（fail-closed）")
	}
	if len(trust.Verifier) == 0 || len(trust.ExemptionAuthority) == 0 {
		return nil, errors.New("trust 配置缺少验签器或豁免机构公钥")
	}
	e := &Engine{
		version:   PolicyVersion,
		allowlist: allowlist,
		verifier:  trust.Verifier,
		exemptKey: trust.ExemptionAuthority,
		signerIDs: map[string]struct{}{},
		clock:     clock,
	}
	for _, pem := range trust.TrustedSignerPEMs {
		pub, err := cryptox.ParsePublicKeyPEM(pem)
		if err != nil {
			return nil, fmt.Errorf("受信签名者公钥: %w", err)
		}
		e.signerIDs[cryptox.KeyID(pub)] = struct{}{}
	}
	var err error
	if e.hash, err = policyHash(allowlist); err != nil {
		return nil, err
	}
	query, err := rego.New(
		rego.Query("result := data.mirrorsec.admission"),
		rego.Module("policy.rego", policy.Source),
	).PrepareForEval(ctx)
	if err != nil {
		return nil, fmt.Errorf("编译 OPA 策略失败: %w", err)
	}
	e.prepared = query
	return e, nil
}

func (e *Engine) Version() string { return e.version }
func (e *Engine) Hash() string    { return e.hash }
func (e *Engine) Allowlist() []model.AllowlistEntry {
	out := make([]model.AllowlistEntry, len(e.allowlist))
	copy(out, e.allowlist)
	return out
}

// Evaluate 执行一次准入评估。调用方负责持久化返回的报告。
func (e *Engine) Evaluate(ctx context.Context, req model.AdmissionRequest) (*model.Report, error) {
	now := e.clock.Now().UTC()

	// 1) 镜像：必须存在且是合法 JSON；摘要真实计算。
	if len(strings.TrimSpace(string(req.Image))) == 0 {
		return nil, errors.New("请求缺少 image（无镜像配置无法评估，fail-closed）")
	}
	imageDigest, err := cryptox.CanonicalDigest(req.Image)
	if err != nil {
		return nil, fmt.Errorf("镜像配置不是合法 JSON: %w", err)
	}
	var imageMap map[string]any
	if err := json.Unmarshal(req.Image, &imageMap); err != nil {
		return nil, fmt.Errorf("解析镜像配置: %w", err)
	}
	repo, _ := imageMap["repository"].(string)
	tag, _ := imageMap["tag"].(string)

	// 2) SBOM：可能缺失（缺证据不通过），存在则真实计算摘要。
	sbomPresent := len(strings.TrimSpace(string(req.SBOM))) > 0
	sbomDigest := ""
	if sbomPresent {
		sbomDigest, err = cryptox.CanonicalDigest(req.SBOM)
		if err != nil {
			return nil, fmt.Errorf("SBOM 不是合法 JSON: %w", err)
		}
	}

	// 3) 验签结果信封：真实验签并分类。
	signedStatus, boundSBOMDigest := e.evaluateVerification(req.Verification, imageDigest, sbomPresent, sbomDigest)
	sbomEvidence := "MISSING"
	if signedStatus == evSigned || signedStatus == evDigestDrift {
		if boundSBOMDigest != "" {
			sbomEvidence = "ATTESTED"
		}
	}

	// 4) 豁免书：逐封真实验签 + 解析到期时间。
	exemptViews, err := e.evaluateExemptions(req.Exemptions, now)
	if err != nil {
		return nil, err
	}

	// 5) 组装 OPA input 并评估。
	input := map[string]any{
		"image":      mergeDigest(imageMap, imageDigest),
		"sbom":       map[string]any{"present": sbomPresent, "digest": sbomDigest},
		"evidence":   map[string]any{"signed": signedStatus, "sbom": sbomEvidence, "boundSBOMDigest": boundSBOMDigest},
		"exemptions": exemptViews,
		"allowlist":  e.allowlist,
		"nowNs":      now.UnixNano(),
	}
	rs, err := e.prepared.Eval(ctx, rego.EvalInput(input))
	if err != nil {
		return nil, fmt.Errorf("OPA 评估失败: %w", err)
	}
	if len(rs) == 0 || len(rs[0].Bindings) == 0 {
		return nil, errors.New("OPA 未产生判定结果")
	}
	binding, _ := rs[0].Bindings["result"].(map[string]any)
	if binding == nil {
		return nil, errors.New("OPA 结果绑定缺少 result")
	}
	decision, _ := binding["decision"].(string)
	if decision == "" {
		return nil, errors.New("OPA 结果缺少 decision")
	}
	findings := parseFindings(binding["findings"])

	requestDigest := e.requestDigest(req, imageDigest)
	report := &model.Report{
		ID:            "rep_" + randHex(12),
		CreatedAt:     now.Format(time.RFC3339Nano),
		PolicyVersion: e.version,
		PolicyHash:    e.hash,
		Repository:    repo,
		Tag:           tag,
		ImageDigest:   imageDigest,
		RequestDigest: requestDigest,
		Decision:      decision,
		Findings:      findings,
		Exemptions:    exemptViews,
	}
	return report, nil
}

// evaluateVerification 对验签结果做真实的密码学核验并分类证据状态。
func (e *Engine) evaluateVerification(raw []byte, imageDigest string, sbomPresent bool, sbomDigest string) (status string, boundSBOM string) {
	if len(strings.TrimSpace(string(raw))) == 0 {
		return evNoVerification, ""
	}
	var env model.Envelope
	if err := json.Unmarshal(raw, &env); err != nil || env.Algorithm != "ed25519" {
		return evBadVerificationEnvelope, ""
	}
	pub, err := cryptox.ParsePublicKeyPEM(e.verifier)
	if err != nil {
		return evBadVerificationEnvelope, ""
	}
	sig, err := cryptox.B64Decode(env.Signature)
	if err != nil {
		return evBadVerificationEnvelope, ""
	}
	// 对载荷重新 canonical 化后验签（防止载荷内部空白/键序被改动）。
	if _, ok, err := cryptox.Verify(pub, json.RawMessage(env.Payload), sig); err != nil || !ok {
		return evBadVerificationEnvelope, ""
	}
	var result model.VerificationResult
	if err := json.Unmarshal(env.Payload, &result); err != nil {
		return evBadVerificationEnvelope, ""
	}
	// 摘要绑定：验签结果必须针对当前镜像，否则是标签漂移/张冠李戴。
	if result.ImageDigest != imageDigest {
		return evDigestDrift, result.SBOMDigest
	}
	if !result.SignatureValid {
		// 验签器给出的失败原因细分（由验签器真实判定）。
		switch {
		case result.SignedBy == "":
			return evUnsigned, result.SBOMDigest
		case !e.trustedSigner(result.SignedBy):
			return evUntrustedKey, result.SBOMDigest
		default:
			return evBadSignature, result.SBOMDigest
		}
	}
	if !e.trustedSigner(result.SignedBy) {
		return evUntrustedKey, result.SBOMDigest
	}
	return evSigned, result.SBOMDigest
}

func (e *Engine) trustedSigner(id string) bool {
	_, ok := e.signerIDs[id]
	return ok
}

// evaluateExemptions 验签每封豁免书并解析到期时间。任何格式错误都不静默丢弃，
// 而是作为“无效豁免”视图交给策略记录为 IMG-EXEMPTION-INVALID。
func (e *Engine) evaluateExemptions(raw []byte, now time.Time) ([]model.ExemptionView, error) {
	if len(strings.TrimSpace(string(raw))) == 0 {
		return []model.ExemptionView{}, nil
	}
	var raws []rawEnvelope
	if err := json.Unmarshal(raw, &raws); err != nil {
		return nil, fmt.Errorf("exemptions 不是合法的信封数组: %w", err)
	}
	pub, err := cryptox.ParsePublicKeyPEM(e.exemptKey)
	if err != nil {
		return nil, fmt.Errorf("豁免机构公钥无效: %w", err)
	}
	views := make([]model.ExemptionView, 0, len(raws))
	for i, renv := range raws {
		view := model.ExemptionView{Digest: "", RuleID: "", ExpiresNs: -1, ID: fmt.Sprintf("envelope[%d]", i)}
		// 逐字段宽容解析：单个信封的 payload base64 损坏不能让整批豁免静默丢失，
		// 而要作为“无效豁免”进入策略，记 IMG-EXEMPTION-INVALID DENY。
		payload, perr := cryptox.B64Decode(renv.Payload)
		var grant model.ExemptionGrant
		if perr == nil {
			perr = json.Unmarshal(payload, &grant)
		}
		if perr == nil {
			view.ID = grant.ID
			view.Digest = grant.ImageDigest
			view.RuleID = grant.RuleID
			view.ExpiresAt = grant.ExpiresAt
			if t, terr := time.Parse(time.RFC3339, grant.ExpiresAt); terr == nil {
				view.ExpiresNs = t.UnixNano()
			}
		}
		sig, serr := cryptox.B64Decode(renv.Signature)
		_, vok, verr := cryptox.Verify(pub, json.RawMessage(payload), sig)
		if perr == nil && serr == nil && verr == nil && vok && renv.Algorithm == "ed25519" {
			view.ValidSignature = true
		} else {
			view.InvalidReason = "豁免书信封验签失败"
		}
		views = append(views, view)
	}
	return views, nil
}

// rawEnvelope 与 model.Envelope 相同，但 payload 先按字符串取，以便逐封解码、
// 单个 base64 损坏时不影响其他豁免的解析。
type rawEnvelope struct {
	Payload   string `json:"payload"`
	Algorithm string `json:"algorithm"`
	KeyID     string `json:"keyId"`
	Signature string `json:"signature"`
}

func (e *Engine) requestDigest(req model.AdmissionRequest, imageDigest string) string {
	parts := []string{imageDigest}
	if d, err := cryptox.CanonicalDigest(req.SBOM); err == nil && d != "" {
		parts = append(parts, d)
	} else {
		parts = append(parts, "sbom:absent")
	}
	if len(req.Verification) > 0 {
		parts = append(parts, cryptox.SHA256Hex(req.Verification))
	} else {
		parts = append(parts, "verification:absent")
	}
	if len(req.Exemptions) > 0 {
		parts = append(parts, cryptox.SHA256Hex(req.Exemptions))
	} else {
		parts = append(parts, "exemptions:absent")
	}
	parts = append(parts, "claimed:"+req.ClaimedDigest)
	return cryptox.SHA256Hex([]byte(strings.Join(parts, "|")))
}

func mergeDigest(m map[string]any, digest string) map[string]any {
	out := make(map[string]any, len(m)+1)
	for k, v := range m {
		out[k] = v
	}
	out["digest"] = digest
	return out
}

func parseFindings(v any) []model.Finding {
	rawList, ok := v.([]any)
	if !ok {
		return []model.Finding{}
	}
	out := make([]model.Finding, 0, len(rawList))
	for _, item := range rawList {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		f := model.Finding{
			RuleID: getString(m, "ruleId"),
			Title:  getString(m, "title"),
			Status: getString(m, "status"),
			Reason: getString(m, "reason"),
		}
		f.ExemptionID, _ = m["exemptionId"].(string)
		out = append(out, f)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].RuleID == out[j].RuleID {
			return out[i].Reason < out[j].Reason
		}
		return out[i].RuleID < out[j].RuleID
	})
	return out
}

func getString(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return s
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// rand.Read 失败说明系统熵源异常，不能退回可预测的 ID。
		panic(fmt.Sprintf("crypto/rand 失败: %v", err))
	}
	return hex.EncodeToString(b)
}
