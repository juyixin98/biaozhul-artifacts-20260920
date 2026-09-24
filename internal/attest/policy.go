package attest

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"
)

// KeyEntry 是策略中的一把可信公钥，带有生效与撤销时间。
type KeyEntry struct {
	KeyID      string
	PublicKey  ed25519.PublicKey
	ValidFrom  time.Time
	ValidUntil time.Time // 零值表示未设置撤销时间
}

// Policy 是验签信任策略：可信 builder、允许的源码仓库、密钥清单及时效。
type Policy struct {
	TrustedBuilders map[string]bool
	AllowedRepos    map[string]bool
	MaxAge          time.Duration // 证明允许的最大年龄（重放窗口）
	Keys            map[string]KeyEntry

	now func() time.Time // 可注入时钟，便于测试
}

// LoadPolicy 从 JSON 字节加载策略。策略文件同样按严格模式解析。
//
// 格式：
//
//	{
//	  "trustedBuilders": ["builder-alice"],
//	  "allowedSourceRepos": ["https://github.com/example/project"],
//	  "maxAttestationAgeSeconds": 600,
//	  "keys": [
//	    {"keyId": "key-2026a", "publicKey": "<base64>",
//	     "validFrom": "2026-01-01T00:00:00Z", "validUntil": "2027-01-01T00:00:00Z"}
//	  ]
//	}
//
// validUntil 可省略或为空字符串，表示该密钥未设撤销时间。
func LoadPolicy(data []byte) (*Policy, error) {
	v, err := ParseStrict(data)
	if err != nil {
		return nil, fmt.Errorf("策略 JSON 解析失败: %v", err)
	}
	root, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("策略必须是 JSON 对象")
	}
	if err := requireKeys(root, "policy",
		"trustedBuilders", "allowedSourceRepos", "maxAttestationAgeSeconds", "keys"); err != nil {
		return nil, err
	}

	p := &Policy{
		TrustedBuilders: make(map[string]bool),
		AllowedRepos:    make(map[string]bool),
		Keys:            make(map[string]KeyEntry),
		now:             time.Now,
	}

	builders, ok := root["trustedBuilders"].([]any)
	if !ok || len(builders) == 0 {
		return nil, fmt.Errorf("trustedBuilders 必须是非空数组")
	}
	for _, b := range builders {
		s, ok := b.(string)
		if !ok || !builderRe.MatchString(s) {
			return nil, fmt.Errorf("trustedBuilders 含非法项 %v", b)
		}
		p.TrustedBuilders[s] = true
	}

	repos, ok := root["allowedSourceRepos"].([]any)
	if !ok || len(repos) == 0 {
		return nil, fmt.Errorf("allowedSourceRepos 必须是非空数组")
	}
	for _, r := range repos {
		s, ok := r.(string)
		if !ok || validRepoURL(s) != nil {
			return nil, fmt.Errorf("allowedSourceRepos 含非法项 %v", r)
		}
		p.AllowedRepos[s] = true
	}

	ageNum, ok := root["maxAttestationAgeSeconds"].(json.Number)
	if !ok {
		return nil, fmt.Errorf("maxAttestationAgeSeconds 必须是整数")
	}
	ageSec, err := ageNum.Int64()
	if err != nil || ageSec <= 0 {
		return nil, fmt.Errorf("maxAttestationAgeSeconds 必须是正整数")
	}
	p.MaxAge = time.Duration(ageSec) * time.Second

	keys, ok := root["keys"].([]any)
	if !ok || len(keys) == 0 {
		return nil, fmt.Errorf("keys 必须是非空数组")
	}
	for _, kv := range keys {
		ko, ok := kv.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("keys 元素必须是对象")
		}
		entry, err := parseKeyEntry(ko)
		if err != nil {
			return nil, err
		}
		if _, dup := p.Keys[entry.KeyID]; dup {
			return nil, fmt.Errorf("策略中 keyId %q 重复", entry.KeyID)
		}
		p.Keys[entry.KeyID] = entry
	}
	return p, nil
}

func parseKeyEntry(obj map[string]any) (KeyEntry, error) {
	var e KeyEntry
	// validUntil 可选，其余字段必需。
	for _, k := range []string{"keyId", "publicKey", "validFrom"} {
		if _, ok := obj[k]; !ok {
			return e, fmt.Errorf("密钥条目缺少字段 %q", k)
		}
	}
	for k := range obj {
		switch k {
		case "keyId", "publicKey", "validFrom", "validUntil":
		default:
			return e, fmt.Errorf("密钥条目包含未知字段 %q", k)
		}
	}

	id, _ := obj["keyId"].(string)
	if !keyIDRe.MatchString(id) {
		return e, fmt.Errorf("密钥条目 keyId 格式非法")
	}
	e.KeyID = id

	pubB64, _ := obj["publicKey"].(string)
	pub, err := base64.StdEncoding.DecodeString(pubB64)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return e, fmt.Errorf("密钥 %q 的 publicKey 必须是 base64 编码的 %d 字节", id, ed25519.PublicKeySize)
	}
	e.PublicKey = ed25519.PublicKey(pub)

	fromStr, _ := obj["validFrom"].(string)
	if e.ValidFrom, err = time.Parse(time.RFC3339, fromStr); err != nil {
		return e, fmt.Errorf("密钥 %q 的 validFrom 非法: %v", id, err)
	}

	if raw, ok := obj["validUntil"]; ok {
		s, ok := raw.(string)
		if !ok {
			return e, fmt.Errorf("密钥 %q 的 validUntil 必须是字符串", id)
		}
		if s != "" {
			if e.ValidUntil, err = time.Parse(time.RFC3339, s); err != nil {
				return e, fmt.Errorf("密钥 %q 的 validUntil 非法: %v", id, err)
			}
			if !e.ValidUntil.After(e.ValidFrom) {
				return e, fmt.Errorf("密钥 %q 的 validUntil 必须晚于 validFrom", id)
			}
		}
	}
	return e, nil
}

// keyUsableAt 判断密钥在时刻 t 是否处于生效窗口内。
func (e KeyEntry) keyUsableAt(t time.Time) bool {
	if t.Before(e.ValidFrom) {
		return false
	}
	if !e.ValidUntil.IsZero() && t.After(e.ValidUntil) {
		return false
	}
	return true
}
