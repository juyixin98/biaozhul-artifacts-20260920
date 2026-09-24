# package mirrorsec.admission
# policy.rego —— 镜像离线准入策略（冻结版本，随二进制嵌入）
#
# 判定原则（fail-closed）：
#   * 必填证据缺失 => UNKNOWN 或 DENY，绝不因字段缺失默认通过；
#   * 豁免只对“具体规则 + 精确镜像摘要 + 未过期 + 验签通过”生效，且不能豁免 UNKNOWN；
#   * 所有规则逐条给出 reason，最终 decision 为 DENY > UNKNOWN > ALLOW。
#
# input 结构由 Go 端组装，证据缺失字段以 null 显式表达（区别于 false）。
package mirrorsec.admission

import rego.v1

# ---------- 入参提取 ----------

cfg       := input.image.config
cfg_user  := object.get(cfg, "user", null)
cfg_priv  := object.get(cfg, "privileged", null)
cfg_cap   := object.get(cfg, "capAdd", [])
base      := object.get(input.image, "baseImage", null)
now_ns    := input.nowNs
allowlist := input.allowlist

# ---------- 豁免 ----------
# 豁免与某条 finding 匹配的充要条件（在叠加规则中内联）：
#   ex.ruleId == f.ruleId 且 ex.digest == 当前镜像摘要；
# 摘要不匹配的豁免自然无法叠加，由豁免合法性规则另行记录。

# exemption_usable: 匹配之外还需验签通过且未过期（边界：到期时刻仍有效）。
exemption_usable(ex) if {
	ex.validSignature == true
	now_ns <= ex.expiresNs
}

# earliest_usable: 同一规则存在多份可用豁免时，只采纳最早到期的一份（再按 id 破平），
# 使每条规则最多产生一行 EXEMPT 结论。
earliest_usable(rule, picked) if {
	some cand in input.exemptions
	cand.ruleId == rule
	cand.digest == input.image.digest
	exemption_usable(cand)
	not earlier_usable(rule, cand)
	picked = cand
}

earlier_usable(rule, ex) if {
	some o in input.exemptions
	o.ruleId == rule
	o.digest == input.image.digest
	exemption_usable(o)
	o.expiresNs < ex.expiresNs
}

earlier_usable(rule, ex) if {
	some o in input.exemptions
	o.ruleId == rule
	o.digest == input.image.digest
	exemption_usable(o)
	o.expiresNs == ex.expiresNs
	o.id < ex.id
}

exemptable_rules := {"IMG-RUN-ROOT", "IMG-PRIVILEGED", "IMG-BASE-ALLOWLIST"}

exemptable(rule) if { rule in exemptable_rules }

# ---------- 规则 1: root 用户 ----------
root_finding contains f if {
	cfg_user == null
	f := {
		"ruleId": "IMG-RUN-ROOT", "title": "禁止以 root 运行",
		"status": "UNKNOWN",
		"reason": "镜像配置缺少 user 字段；无法确认非 root，按缺证据不通过处理",
	}
}

root_finding contains f if {
	is_string(cfg_user)
	u := norm_user(cfg_user)
	u == ""
	f := {
		"ruleId": "IMG-RUN-ROOT", "title": "禁止以 root 运行",
		"status": "DENY",
		"reason": "user 为空字符串，容器运行时将默认以 root(uid 0) 运行",
	}
}

root_finding contains f if {
	is_string(cfg_user)
	u := norm_user(cfg_user)
	u != ""
	root_like(u)
	f := {
		"ruleId": "IMG-RUN-ROOT", "title": "禁止以 root 运行",
		"status": "DENY",
		"reason": sprintf("user=%q 解析为 uid 0 (root)", [cfg_user]),
	}
}

root_finding contains f if {
	is_string(cfg_user)
	u := norm_user(cfg_user)
	u != ""
	not root_like(u)
	f := {
		"ruleId": "IMG-RUN-ROOT", "title": "禁止以 root 运行",
		"status": "ALLOW",
		"reason": sprintf("user=%q 非 root", [cfg_user]),
	}
}

norm_user(s) := lower(trim(s, " \t\r\n"))

# root: "root"、"0"、"0:0"、"0:1234"、"root:gid"；"1000:0" 不算。
root_like(u) if { split(u, ":")[0] == "root" }
root_like(u) if { split(u, ":")[0] == "0" }

# ---------- 规则 2: 特权要求 ----------
priv_finding contains f if {
	cfg_priv == null
	f := {
		"ruleId": "IMG-PRIVILEGED", "title": "禁止特权容器",
		"status": "UNKNOWN",
		"reason": "镜像配置缺少 privileged 字段；按缺证据不通过处理",
	}
}

priv_finding contains f if {
	cfg_priv == true
	f := {
		"ruleId": "IMG-PRIVILEGED", "title": "禁止特权容器",
		"status": "DENY",
		"reason": "privileged=true，容器将获得宿主机全部能力",
	}
}

priv_finding contains f if {
	cfg_priv == false
	cap_all
	f := {
		"ruleId": "IMG-PRIVILEGED", "title": "禁止特权容器",
		"status": "DENY",
		"reason": "capAdd 包含 ALL，等同特权提升，即使 privileged=false 也拒绝",
	}
}

priv_finding contains f if {
	cfg_priv == false
	not cap_all
	f := {
		"ruleId": "IMG-PRIVILEGED", "title": "禁止特权容器",
		"status": "ALLOW",
		"reason": "privileged=false 且未申请 ALL 能力集",
	}
}

cap_all if {
	some c in cfg_cap
	upper(trim(c, " \t\r\n")) == "ALL"
}

# ---------- 规则 3: 基础镜像允许列表（仓库 + 摘要精确匹配）----------
base_finding contains f if {
	base == null
	f := {
		"ruleId": "IMG-BASE-ALLOWLIST", "title": "基础镜像允许列表",
		"status": "UNKNOWN",
		"reason": "镜像配置缺少 baseImage；无法核对允许列表，按缺证据不通过处理",
	}
}

base_finding contains f if {
	base != null
	not startswith(object.get(base, "digest", ""), "sha256:")
	f := {
		"ruleId": "IMG-BASE-ALLOWLIST", "title": "基础镜像允许列表",
		"status": "UNKNOWN",
		"reason": "baseImage.digest 缺失或不是 sha256 摘要；可变 tag 不能作为允许依据",
	}
}

base_finding contains f if {
	base != null
	startswith(object.get(base, "digest", ""), "sha256:")
	object.get(base, "repository", "") == ""
	f := {
		"ruleId": "IMG-BASE-ALLOWLIST", "title": "基础镜像允许列表",
		"status": "UNKNOWN",
		"reason": "baseImage.repository 缺失，无法匹配允许列表",
	}
}

base_finding contains f if {
	base != null
	repo := object.get(base, "repository", "")
	repo != ""
	startswith(object.get(base, "digest", ""), "sha256:")
	not allowlist_hit(repo, base.digest)
	f := {
		"ruleId": "IMG-BASE-ALLOWLIST", "title": "基础镜像允许列表",
		"status": "DENY",
		"reason": sprintf("基础镜像 %s@%s 不在允许列表中", [repo, base.digest]),
	}
}

base_finding contains f if {
	base != null
	allowlist_hit(base.repository, base.digest)
	f := {
		"ruleId": "IMG-BASE-ALLOWLIST", "title": "基础镜像允许列表",
		"status": "ALLOW",
		"reason": sprintf("基础镜像 %s@%s 命中允许列表", [base.repository, base.digest]),
	}
}

allowlist_hit(repo, digest) if {
	some e in allowlist
	e.repository == repo
	e.digest == digest
}

# ---------- 规则 4: SBOM 证据 ----------
sbom_finding contains f if {
	input.sbom.present == false
	f := {
		"ruleId": "IMG-SBOM", "title": "SBOM 物料清单",
		"status": "UNKNOWN",
		"reason": "请求缺少 SBOM；无物料清单证据，按缺证据不通过处理",
	}
}

# 证据绑定：SBOM 实际摘要必须与验签结果背书的 SBOM 摘要一致。
sbom_finding contains f if {
	input.sbom.present
	input.evidence.boundSBOMDigest != ""
	input.sbom.digest != input.evidence.boundSBOMDigest
	f := {
		"ruleId": "IMG-DIGEST-BIND", "title": "证据摘要绑定",
		"status": "DENY",
		"reason": sprintf("SBOM 实际摘要 %s 与验签结果绑定的 %s 不一致（疑似替换/标签漂移）",
			[input.sbom.digest, input.evidence.boundSBOMDigest]),
	}
}

sbom_finding contains f if {
	input.sbom.present
	input.evidence.sbom == "ATTESTED"
	input.evidence.boundSBOMDigest == input.sbom.digest
	f := {
		"ruleId": "IMG-SBOM", "title": "SBOM 物料清单",
		"status": "ALLOW",
		"reason": sprintf("SBOM 已随验签结果背书，摘要 %s", [input.sbom.digest]),
	}
}

sbom_finding contains f if {
	input.sbom.present
	input.evidence.sbom == "ATTESTED"
	input.evidence.boundSBOMDigest == ""
	f := {
		"ruleId": "IMG-SBOM", "title": "SBOM 物料清单",
		"status": "UNKNOWN",
		"reason": "验签结果未携带 SBOM 摘要绑定，无法确认 SBOM 身份",
	}
}

sbom_finding contains f if {
	input.sbom.present
	input.evidence.sbom != "ATTESTED"
	f := {
		"ruleId": "IMG-SBOM", "title": "SBOM 物料清单",
		"status": "UNKNOWN",
		"reason": sprintf("SBOM 存在但缺少有效背书（证据状态=%s）", [input.evidence.sbom]),
	}
}

# ---------- 规则 5: 镜像签名（由本地验签器结论裁决）----------
signed_meta := {
	"SIGNED":                    {"status": "ALLOW", "reason": "本地验签器确认镜像签名有效，验签结果由受信验签器密钥背书"},
	"UNSIGNED":                  {"status": "DENY", "reason": "验签器报告镜像缺少有效签名"},
	"BAD_SIGNATURE":             {"status": "DENY", "reason": "验签器报告镜像签名校验失败（签名与摘要不匹配）"},
	"UNTRUSTED_KEY":             {"status": "DENY", "reason": "镜像签名密钥不在受信签名者列表中"},
	"NO_VERIFICATION":           {"status": "UNKNOWN", "reason": "请求未附带本地验签器结果；无签名证据，按缺证据不通过处理"},
	"BAD_VERIFICATION_ENVELOPE": {"status": "DENY", "reason": "验签结果信封验签失败：结果可能被篡改或非受信验签器签发"},
}

signed_finding contains f if {
	input.evidence.signed != "DIGEST_DRIFT"
	meta := signed_meta[input.evidence.signed]
	f := {
		"ruleId": "IMG-SIGNED", "title": "镜像签名校验",
		"status": meta.status, "reason": meta.reason,
	}
}

signed_finding contains f if {
	input.evidence.signed == "DIGEST_DRIFT"
	f := {
		"ruleId": "IMG-DIGEST-BIND", "title": "证据摘要绑定",
		"status": "DENY",
		"reason": "验签结果绑定的镜像摘要与实际镜像摘要不一致（标签漂移/内容替换）",
	}
}

# ---------- 规则 6: 豁免书自身合法性（篡改/越权/挪用/过期）----------
bad_exempt_finding contains f if {
	some ex in input.exemptions
	ex.validSignature == false
	f := {
		"ruleId": "IMG-EXEMPTION-INVALID", "title": "豁免书合法性",
		"status": "DENY",
		"reason": sprintf("豁免书 %s 验签失败或已被篡改：拒绝采纳", [ex.id]),
	}
}

bad_exempt_finding contains f if {
	some ex in input.exemptions
	ex.validSignature
	not exemptable(ex.ruleId)
	f := {
		"ruleId": "IMG-EXEMPTION-INVALID", "title": "豁免书合法性",
		"status": "DENY",
		"reason": sprintf("豁免书 %s 试图豁免不可豁免的规则 %s（签名/缺证据/豁免规则本身均不可豁免）",
			[ex.id, ex.ruleId]),
	}
}

bad_exempt_finding contains f if {
	some ex in input.exemptions
	ex.validSignature
	exemptable(ex.ruleId)
	ex.digest != input.image.digest
	f := {
		"ruleId": "IMG-EXEMPTION-INVALID", "title": "豁免书合法性",
		"status": "DENY",
		"reason": sprintf("豁免书 %s 绑定摘要 %s，与当前镜像 %s 不符（禁止跨镜像/跨标签挪用）",
			[ex.id, ex.digest, input.image.digest]),
	}
}

bad_exempt_finding contains f if {
	some ex in input.exemptions
	ex.validSignature
	exemptable(ex.ruleId)
	ex.digest == input.image.digest
	now_ns > ex.expiresNs
	f := {
		"ruleId": "IMG-EXEMPTION-INVALID", "title": "豁免书合法性",
		"status": "DENY",
		"reason": sprintf("豁免书 %s 已于 %s 过期（边界判定：到期时刻仍有效，过期一秒即失效）",
			[ex.id, ex.expiresAt]),
	}
}

# ---------- 豁免叠加：仅对 DENY 生效，UNKNOWN 不可豁免 ----------
base_findings := root_finding | priv_finding | base_finding | sbom_finding | signed_finding

findings contains out if {
	some f in base_findings
	some ex in input.exemptions
	ex.ruleId == f.ruleId
	ex.digest == input.image.digest
	f.status == "DENY"
	earliest_usable(f.ruleId, ex)
	out := object.union(f, {
		"status": "EXEMPT",
		"exemptionId": ex.id,
		"reason": sprintf("%s（已被豁免书 %s 豁免，有效期至 %s）", [f.reason, ex.id, ex.expiresAt]),
	})
}

findings contains out if {
	some f in base_findings
	not exempted(f)
	out := f
}

exempted(f) if {
	some ex in input.exemptions
	ex.ruleId == f.ruleId
	ex.digest == input.image.digest
	f.status == "DENY"
	earliest_usable(f.ruleId, ex)
}

findings contains f if { some f in bad_exempt_finding }

# ---------- 汇总：DENY > UNKNOWN > ALLOW（EXEMPT 视为通过）----------
decision := "DENY" if {
	some f in findings
	f.status == "DENY"
} else := "UNKNOWN" if {
	some f in findings
	f.status == "UNKNOWN"
} else := "ALLOW"
