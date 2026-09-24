//! 验证引擎：对一条证明信封执行有序的逐条策略判定，产出结构化报告。
//!
//! 判定始终“跑完所有检查”，而不是遇到第一个失败就返回，这样验证报告
//! 能同时呈现多条策略证据（例如输出替换场景下，签名失败与输出摘要不一致
//! 会同时被记录）。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::crypto::verify_signature;
use crate::model::{Envelope, Statement};
use crate::policy::Policy;

/// `POST /v1/verify` 请求体。
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VerifyRequest {
    /// 待验证的签名证明信封。
    pub envelope: Envelope,
    /// 验证方（软件消费者）期望证明覆盖的源提交。
    pub expected_source_commit: crate::model::SourceCommit,
    /// 期望证明覆盖的材料集合（按 URI 去重）。
    #[serde(default)]
    pub expected_materials: Vec<crate::model::Material>,
    /// 期望的构建输出摘要。用于检出“构建后替换输出”：
    /// 证明自身合法，但证明中的 output 与消费者实际拿到的制品不一致。
    pub expected_output: Option<crate::model::Output>,
}

/// 单个策略检查点的判定。
#[derive(Clone, Debug, Serialize)]
pub struct Check {
    pub id: &'static str,
    pub name: &'static str,
    pub status: CheckStatus,
    /// 机器可读细节（如命中的 URI / 摘要）。
    pub detail: String,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "UPPERCASE")]
pub enum CheckStatus {
    Pass,
    Fail,
}

/// 整份验证报告。
#[derive(Clone, Debug, Serialize)]
pub struct VerificationReport {
    pub verdict: Verdict,
    pub builder_id: String,
    pub checks: Vec<Check>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Verdict {
    /// 全部检查通过。
    Allowed,
    /// 任一检查失败。
    Deny,
}

/// 执行完整验证。
pub fn evaluate(policy: &Policy, req: &VerifyRequest) -> VerificationReport {
    let env = &req.envelope;
    let st = &env.payload;

    let checks: Vec<Check> = vec![
        check_known_digest_algorithms(st),
        check_trusted_builder(policy, st),
        check_signing_key_binding(policy, st, &env.key_id),
        check_source_repo(policy, st, &req.expected_source_commit),
        check_material_origins(policy, st),
        check_materials_complete(st, &req.expected_materials),
        check_output_matches(st, req.expected_output.as_ref()),
        check_signature(st, &env.key_id, &env.signature_hex),
    ];

    let verdict = if checks.iter().all(|c| c.status == CheckStatus::Pass) {
        Verdict::Allowed
    } else {
        Verdict::Deny
    };

    VerificationReport {
        verdict,
        builder_id: st.builder_id.clone(),
        checks,
    }
}

fn check_known_digest_algorithms(st: &Statement) -> Check {
    let mut bad = Vec::new();
    for m in &st.materials {
        if m.digest.alg != "sha256" {
            bad.push(format!("{}:{}", m.uri, m.digest.alg));
        }
    }
    if st.output.digest.alg != "sha256" {
        bad.push(format!("output:{}", st.output.digest.alg));
    }
    Check {
        id: "digest_algorithms",
        name: "摘要算法受支持（sha256）",
        status: if bad.is_empty() {
            CheckStatus::Pass
        } else {
            CheckStatus::Fail
        },
        detail: if bad.is_empty() {
            "所有摘要均为 sha256".to_string()
        } else {
            format!("不支持的摘要算法: {}", bad.join(", "))
        },
    }
}

fn check_trusted_builder(policy: &Policy, st: &Statement) -> Check {
    let pass = policy.is_trusted_builder(&st.builder_id);
    Check {
        id: "trusted_builder",
        name: "构建器在可信注册表中",
        status: status(pass),
        detail: if pass {
            format!("构建器 {} 为可信构建器", st.builder_id)
        } else {
            format!("构建器 {} 不在可信注册表中", st.builder_id)
        },
    }
}

fn check_signing_key_binding(policy: &Policy, st: &Statement, key_id: &str) -> Check {
    match policy.trusted_builders.get(&st.builder_id) {
        Some(entry) => {
            let pass = entry.public_key_hex.eq_ignore_ascii_case(key_id);
            Check {
                id: "signing_key_binding",
                name: "信封公钥与构建器注册公钥一致",
                status: status(pass),
                detail: if pass {
                    format!("信封公钥与构建器 {} 的注册公钥一致", st.builder_id)
                } else {
                    format!(
                        "信封公钥 {} 与 {} 注册公钥 {} 不符（疑似跨构建器复用证明）",
                        key_id, st.builder_id, entry.public_key_hex
                    )
                },
            }
        }
        None => Check {
            id: "signing_key_binding",
            name: "信封公钥与构建器注册公钥一致",
            status: CheckStatus::Fail,
            detail: format!("构建器 {} 不受信任，无法比对注册公钥", st.builder_id),
        },
    }
}

fn check_source_repo(
    policy: &Policy,
    st: &Statement,
    expected: &crate::model::SourceCommit,
) -> Check {
    let repo_allowed = policy.is_source_repo_allowed(&st.source_commit.repo);
    let matches_expectation = st.source_commit == *expected;

    let (pass, detail) = match (repo_allowed, matches_expectation) {
        (true, true) => (
            true,
            format!(
                "源仓库 {} 在允许列表中，且与期望提交 {} 一致",
                st.source_commit.repo, st.source_commit.revision
            ),
        ),
        (true, false) => (
            false,
            format!(
                "证明源提交 {}/{} 与期望 {}/{} 不一致",
                st.source_commit.repo, st.source_commit.revision, expected.repo, expected.revision
            ),
        ),
        (false, _) => (
            false,
            format!("源仓库 {} 不在允许列表中", st.source_commit.repo),
        ),
    };
    Check {
        id: "source_commit",
        name: "源仓库受允许且源提交匹配",
        status: status(pass),
        detail,
    }
}

fn check_material_origins(policy: &Policy, st: &Statement) -> Check {
    let disallowed: Vec<String> = st
        .materials
        .iter()
        .filter(|m| !policy.is_origin_allowed(&m.uri))
        .map(|m| m.uri.clone())
        .collect();
    Check {
        id: "material_origins",
        name: "材料来源在允许来源内",
        status: status(disallowed.is_empty()),
        detail: if disallowed.is_empty() {
            format!("全部 {} 项材料来源均被允许", st.materials.len())
        } else {
            format!("来源不被允许的材料: {}", disallowed.join(", "))
        },
    }
}

/// 期望材料集合 vs 证明材料集合的逐项比对，按 URI 建立索引。
fn check_materials_complete(st: &Statement, expected: &[crate::model::Material]) -> Check {
    let attested: BTreeMap<&str, &crate::model::Digest> = st
        .materials
        .iter()
        .map(|m| (m.uri.as_str(), &m.digest))
        .collect();
    let wanted: BTreeMap<&str, &crate::model::Digest> = expected
        .iter()
        .map(|m| (m.uri.as_str(), &m.digest))
        .collect();

    let mut missing: Vec<&str> = Vec::new();
    let mut altered: Vec<String> = Vec::new();
    for (uri, want_digest) in &wanted {
        match attested.get(*uri) {
            None => missing.push(uri),
            Some(got) if **got != **want_digest => altered.push(format!(
                "{uri}: 期望 {}:{}, 证明 {}:{}",
                want_digest.alg, want_digest.hex, got.alg, got.hex
            )),
            Some(_) => {}
        }
    }
    let unexpected: Vec<&str> = attested
        .keys()
        .filter(|uri| !wanted.contains_key(*uri))
        .copied()
        .collect();

    let mut problems: Vec<String> = Vec::new();
    if !missing.is_empty() {
        problems.push(format!("证明缺失材料: {}", missing.join(", ")));
    }
    if !altered.is_empty() {
        problems.push(format!("材料摘要不一致: {}", altered.join("; ")));
    }
    if !unexpected.is_empty() {
        problems.push(format!("证明包含多余材料: {}", unexpected.join(", ")));
    }

    Check {
        id: "materials_complete",
        name: "材料集合完整且摘要一致",
        status: status(problems.is_empty()),
        detail: if problems.is_empty() {
            format!("证明覆盖全部 {} 项期望材料，摘要一致", wanted.len())
        } else {
            problems.join("；")
        },
    }
}

fn check_output_matches(st: &Statement, expected: Option<&crate::model::Output>) -> Check {
    match expected {
        None => Check {
            id: "output_match",
            name: "输出摘要与期望制品一致",
            status: CheckStatus::Pass,
            detail: "请求未提供期望输出，跳过输出比对".to_string(),
        },
        Some(want) => {
            let pass = st.output == *want;
            Check {
                id: "output_match",
                name: "输出摘要与期望制品一致",
                status: status(pass),
                detail: if pass {
                    format!("输出摘要 {}:{} 一致", want.digest.alg, want.digest.hex)
                } else {
                    format!(
                        "输出被替换：证明 {}:{} ≠ 实际制品 {}:{}",
                        st.output.digest.alg,
                        st.output.digest.hex,
                        want.digest.alg,
                        want.digest.hex
                    )
                },
            }
        }
    }
}

fn check_signature(st: &Statement, key_id: &str, sig_hex: &str) -> Check {
    match verify_signature(key_id, st, sig_hex) {
        Ok(()) => Check {
            id: "signature",
            name: "Ed25519 签名验证通过",
            status: CheckStatus::Pass,
            detail: "证明规范化字节可被信封公钥验签".to_string(),
        },
        Err(e) => Check {
            id: "signature",
            name: "Ed25519 签名验证通过",
            status: CheckStatus::Fail,
            detail: format!("验签失败: {e:?}"),
        },
    }
}

fn status(pass: bool) -> CheckStatus {
    if pass {
        CheckStatus::Pass
    } else {
        CheckStatus::Fail
    }
}
