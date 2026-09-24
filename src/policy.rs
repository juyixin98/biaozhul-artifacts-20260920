//! 验证策略与本地材料库（夹具）。

use crate::crypto::sha256;
use crate::types::{CheckResult, Envelope, Statement, VerificationReport, VerifyRequest};
use anyhow::{Context, Result};
use base64::Engine as _;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::crypto::KeyRegistry;

/// 验证策略：限定可信构建器与允许的材料来源。
#[derive(Debug, Clone)]
pub struct Policy {
    /// 允许为*本项目*出具证明的构建器 id 集合。
    pub trusted_builders: HashSet<String>,
    /// 材料 URI 前缀白名单（源仓库、依赖分发地址等）。
    pub allowed_material_prefixes: Vec<String>,
    /// 允许的源仓库地址集合（精确匹配）。
    pub allowed_source_repositories: HashSet<String>,
}

impl Policy {
    pub fn uri_allowed(&self, uri: &str) -> bool {
        self.allowed_material_prefixes
            .iter()
            .any(|prefix| uri.starts_with(prefix.as_str()))
    }
}

/// 本地材料库：`fixtures/materials/MANIFEST.json` 声明 URI -> 本地文件，
/// 服务启动时逐文件计算摘要（模拟验证者已拉取到本地的材料）。
#[derive(Debug, Clone)]
pub struct MaterialStore {
    /// uri -> (sha256, 本地路径)
    entries: HashMap<String, ([u8; 32], PathBuf)>,
}

impl MaterialStore {
    pub fn load_from_dir(dir: &Path) -> Result<Self> {
        let manifest_path = dir.join("MANIFEST.json");
        let manifest: HashMap<String, String> = serde_json::from_slice(
            &std::fs::read(&manifest_path)
                .with_context(|| format!("read material manifest {}", manifest_path.display()))?,
        )
        .context("parse materials/MANIFEST.json (expects {\"uri\": \"relative_path\"})")?;

        let mut entries = HashMap::new();
        for (uri, rel) in manifest {
            let path = dir.join(&rel);
            let bytes = std::fs::read(&path)
                .with_context(|| format!("read material file {}", path.display()))?;
            entries.insert(uri, (sha256(&bytes), path));
        }
        Ok(MaterialStore { entries })
    }

    /// 按 URI 查找本地材料摘要；不存在即“材料缺失”。
    pub fn digest_of(&self, uri: &str) -> Option<[u8; 32]> {
        self.entries.get(uri).map(|(digest, _)| *digest)
    }

    /// 本地材料库中全部材料 URI（排序后，便于展示）。
    pub fn uris(&self) -> Vec<String> {
        let mut uris: Vec<String> = self.entries.keys().cloned().collect();
        uris.sort();
        uris
    }
}

fn passed(check: &str, detail: impl Into<String>) -> CheckResult {
    CheckResult {
        check: check.to_string(),
        passed: true,
        code: "PASS".to_string(),
        detail: detail.into(),
    }
}

fn failed(check: &str, code: &str, detail: impl Into<String>) -> CheckResult {
    CheckResult {
        check: check.to_string(),
        passed: false,
        code: code.to_string(),
        detail: detail.into(),
    }
}

/// 陈述 `subject` 必须唯一且与谓词中的 output 一致（证明内部绑定）。
fn check_subject_binding(statement: &Statement) -> CheckResult {
    match statement.subject.as_slice() {
        [single] if single == &statement.predicate.output => passed(
            "subject_binding",
            "statement subject 与谓词 output 名称及摘要一致",
        ),
        [single] => failed(
            "subject_binding",
            "SUBJECT_OUTPUT_MISMATCH",
            format!(
                "subject 为 {}:{}，谓词 output 为 {}:{}",
                single.name,
                single.digest.as_hex(),
                statement.predicate.output.name,
                statement.predicate.output.digest.as_hex()
            ),
        ),
        n => failed(
            "subject_binding",
            "BAD_SUBJECT",
            format!("期望恰好 1 个 subject，实际为 {}", n.len()),
        ),
    }
}

fn check_trusted_builder(statement: &Statement, policy: &Policy) -> CheckResult {
    let id = &statement.predicate.builder_id;
    if policy.trusted_builders.contains(id) {
        passed("trusted_builder", format!("构建器 `{id}` 在可信名单内"))
    } else {
        failed(
            "trusted_builder",
            "UNTRUSTED_BUILDER",
            format!("构建器 `{id}` 不在本项目可信构建器名单内"),
        )
    }
}

/// 构建器身份绑定：签名者 == 陈述声明的 builder_id；
/// 若请求给出实际执行构建的构建器，则还必须与之相等
/// （用于识别“A 构建器的真证明被拿去给 B 构建器的产物背书”）。
fn check_builder_binding(
    statement: &Statement,
    signer_keyid: &str,
    actual_builder_id: Option<&str>,
) -> CheckResult {
    let claimed = &statement.predicate.builder_id;
    if signer_keyid != claimed {
        return failed(
            "builder_binding",
            "SIGNER_BUILDER_MISMATCH",
            format!("证明由 `{signer_keyid}` 签名，但陈述声称构建器为 `{claimed}`"),
        );
    }
    if let Some(actual) = actual_builder_id {
        if actual != claimed {
            return failed(
                "builder_binding",
                "CROSS_BUILDER_REUSE",
                format!(
                    "证明由可信构建器 `{claimed}` 出具，但本次待准入构建实际由 `{actual}` 执行——证明被跨构建器复用"
                ),
            );
        }
    }
    passed(
        "builder_binding",
        "签名者、陈述声明构建器与实际构建器一致",
    )
}

fn check_source_commit(statement: &Statement, policy: &Policy) -> CheckResult {
    let sc = &statement.predicate.source_commit;
    if !policy.allowed_source_repositories.contains(&sc.repository) {
        return failed(
            "source_commit_binding",
            "SOURCE_REPO_NOT_ALLOWED",
            format!("源仓库 `{}` 不在允许名单内", sc.repository),
        );
    }
    let commit_ok = sc.ref_commit.len() == 40
        && sc.ref_commit.chars().all(|c| c.is_ascii_hexdigit());
    if !commit_ok {
        return failed(
            "source_commit_binding",
            "BAD_COMMIT_PIN",
            format!("`{}` 不是 40 位十六进制的不可变提交哈希", sc.ref_commit),
        );
    }
    // 材料中必须存在指向该仓库@提交 的源材料，证明源提交确实被消费。
    let wanted_prefix = format!("git+{}@{}", sc.repository, sc.ref_commit);
    let bound = statement
        .predicate
        .materials
        .iter()
        .any(|m| m.uri.starts_with(&wanted_prefix));
    if !bound {
        return failed(
            "source_commit_binding",
            "SOURCE_NOT_IN_MATERIALS",
            format!("材料列表中缺少以 `{wanted_prefix}` 标识的源提交材料"),
        );
    }
    passed(
        "source_commit_binding",
        format!("源提交固定为 {} 的 {}", sc.ref_commit, sc.repository),
    )
}

/// 材料齐备性：陈述中的每份材料都必须在本地材料库中存在且摘要一致。
fn check_materials_present(statement: &Statement, store: &MaterialStore) -> CheckResult {
    let mut missing = Vec::new();
    let mut mismatched = Vec::new();
    for m in &statement.predicate.materials {
        match store.digest_of(&m.uri) {
            None => missing.push(m.uri.clone()),
            Some(actual) if actual != m.digest.0 => mismatched.push(format!(
                "{}: 证明 {} vs 本地 {}",
                m.uri,
                m.digest.as_hex(),
                hex::encode(actual)
            )),
            Some(_) => {}
        }
    }
    if !missing.is_empty() {
        failed(
            "materials_complete",
            "MATERIAL_MISSING",
            format!("本地材料库缺少 {} 份材料: {}", missing.len(), missing.join("; ")),
        )
    } else if !mismatched.is_empty() {
        failed(
            "materials_complete",
            "MATERIAL_DIGEST_MISMATCH",
            mismatched.join("; "),
        )
    } else {
        passed(
            "materials_complete",
            format!("全部 {} 份材料均在本地且摘要一致", statement.predicate.materials.len()),
        )
    }
}

/// 材料来源白名单。
fn check_material_sources(statement: &Statement, policy: &Policy) -> CheckResult {
    let denied: Vec<&str> = statement
        .predicate
        .materials
        .iter()
        .map(|m| m.uri.as_str())
        .filter(|uri| !policy.uri_allowed(uri))
        .collect();
    if denied.is_empty() {
        passed(
            "material_sources_allowed",
            "全部材料 URI 均命中来源白名单前缀",
        )
    } else {
        failed(
            "material_sources_allowed",
            "MATERIAL_SOURCE_DENIED",
            format!("材料来源不在白名单: {}", denied.join("; ")),
        )
    }
}

fn check_output_digest(statement: &Statement, actual_hex: Option<&str>) -> CheckResult {
    let claimed = statement.predicate.output.digest.as_hex();
    match actual_hex {
        None => failed(
            "output_digest",
            "ARTIFACT_NOT_SUPPLIED",
            "请求未提供待验证产物或其实际摘要，无法比对输出摘要",
        ),
        Some(actual_raw) => {
            let normalized = actual_raw.trim().to_lowercase();
            if normalized == claimed {
                passed("output_digest", format!("实际产物 SHA-256 = {claimed}，与证明一致"))
            } else {
                failed(
                    "output_digest",
                    "OUTPUT_DIGEST_MISMATCH",
                    format!("证明声称 {claimed}，实际产物为 {normalized}：输出已被替换"),
                )
            }
        }
    }
}

/// 从请求中解析验证者侧实际产物摘要（原始字节 / base64 / 直接给 hex）。
pub fn resolve_actual_output_digest(req: &VerifyRequest) -> Result<Option<[u8; 32]>> {
    if let Some(b64) = &req.artifact_base64 {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .context("artifact_base64 不是合法 base64")?;
        return Ok(Some(sha256(&bytes)));
    }
    if let Some(raw) = &req.artifact_bytes {
        return Ok(Some(sha256(raw.as_bytes())));
    }
    if let Some(hex_str) = &req.actual_output_digest {
        let v = hex::decode(hex_str.trim()).context("actual_output_digest 不是合法 hex")?;
        let arr: [u8; 32] = v
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("actual_output_digest 必须为 32 字节"))?;
        return Ok(Some(arr));
    }
    Ok(None)
}

/// 策略验证器，聚合密钥注册表、策略与本地材料库。
pub struct Verifier {
    pub keys: KeyRegistry,
    pub policy: Policy,
    pub materials: MaterialStore,
    pub fixtures_dir: PathBuf,
}

impl Verifier {
    /// 读取夹具中的签名证明信封。
    pub fn load_proof_envelope(&self, rel: &str) -> Result<Envelope> {
        let path = self.fixtures_dir.join("proofs").join(rel);
        let canonical = path.canonicalize().with_context(|| format!("定位证明文件 {rel}"))?;
        let root = self
            .fixtures_dir
            .canonicalize()
            .context("定位 fixtures 目录")?;
        if !canonical.starts_with(&root) {
            anyhow::bail!("非法 proof_path：越出 fixtures 目录");
        }
        serde_json::from_slice(
            &std::fs::read(&canonical).with_context(|| format!("读取证明文件 {}", canonical.display()))?,
        )
        .context("证明文件不是合法信封 JSON")
    }

    /// 执行全部检查；无论前面是否失败都跑完，逐条给出判定。
    pub fn verify(&self, envelope: &Envelope, req: &VerifyRequest) -> VerificationReport {
        let mut checks: Vec<CheckResult> = Vec::new();

        let (actual_hex, output_request_error) = match resolve_actual_output_digest(req) {
            Ok(d) => (d.map(hex::encode), None),
            Err(e) => (
                None,
                Some(failed("output_digest", "BAD_REQUEST", e.to_string())),
            ),
        };

        // 1. 密码学签名验证（同时解析陈述）。
        let verified = self.keys.verify_envelope(envelope);
        let (signer_keyid, statement) = match verified {
            Ok((keyid, st)) => {
                checks.push(passed(
                    "signature",
                    format!("Ed25519 签名有效，签名密钥 `{keyid}`"),
                ));
                (Some(keyid), Some(st))
            }
            Err(e) => {
                checks.push(failed("signature", "INVALID_SIGNATURE", e.to_string()));
                (None, None)
            }
        };

        if let Some(st) = &statement {
            // 2. 证明内部一致性。
            checks.push(check_subject_binding(st));
            // 3. 构建器身份与可信名单。
            checks.push(check_trusted_builder(st, &self.policy));
            checks.push(check_builder_binding(
                st,
                signer_keyid.as_deref().unwrap_or(""),
                req.actual_builder_id.as_deref(),
            ));
            // 4. 源提交绑定与仓库白名单。
            checks.push(check_source_commit(st, &self.policy));
            // 5. 材料齐备性（本地夹具）与来源白名单。
            checks.push(check_materials_present(st, &self.materials));
            checks.push(check_material_sources(st, &self.policy));
            // 6. 输出摘要比对。
            checks.push(match output_request_error {
                Some(err) => err,
                None => check_output_digest(st, actual_hex.as_deref()),
            });
        } else {
            // 签名/解析不过时，无法运行依赖陈述内容的策略。
            for (name, code) in [
                ("subject_binding", "SKIPPED"),
                ("trusted_builder", "SKIPPED"),
                ("builder_binding", "SKIPPED"),
                ("source_commit_binding", "SKIPPED"),
                ("materials_complete", "SKIPPED"),
                ("material_sources_allowed", "SKIPPED"),
                ("output_digest", "SKIPPED"),
            ] {
                checks.push(failed(name, code, "陈述未能通过签名验证，跳过该检查"));
            }
        }

        let accepted = checks.iter().all(|c| c.passed);
        VerificationReport {
            accepted,
            checks,
            statement,
            signer_keyid,
            actual_output_digest: actual_hex,
        }
    }
}
