//! 证明 / 信封 / 请求的数据模型（SLSA v1 风格的 in-toto 陈述）。

use serde::{Deserialize, Serialize};

use base64::Engine as _;

/// SHA-256 摘要，序列化为 `{"sha256": "<hex>"}`（in-toto DigestSet 惯例）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DigestSet(
    #[serde(with = "digest_map")]
    pub [u8; 32],
);

impl DigestSet {
    pub fn new(bytes: [u8; 32]) -> Self {
        DigestSet(bytes)
    }
    pub fn as_hex(&self) -> String {
        hex::encode(self.0)
    }
}

mod digest_map {
    use serde::de::Error as _;
    use serde::{Deserialize, Deserializer, Serialize, Serializer};
    use std::collections::BTreeMap;

    pub fn serialize<S>(bytes: &[u8; 32], serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut m = BTreeMap::new();
        m.insert("sha256".to_string(), hex::encode(bytes));
        m.serialize(serializer)
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<[u8; 32], D::Error>
    where
        D: Deserializer<'de>,
    {
        let m = BTreeMap::<String, String>::deserialize(deserializer)?;
        let hex_str = m
            .get("sha256")
            .ok_or_else(|| D::Error::custom("digest set missing `sha256`"))?;
        let v = hex::decode(hex_str).map_err(D::Error::custom)?;
        v.try_into()
            .map_err(|_| D::Error::custom("sha256 digest must be 32 bytes"))
    }
}

/// 构建所消费的一份材料（源文件、依赖等）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Material {
    /// 材料 URI，例如 `git+https://example.com/org/repo` 或
    /// `https://deps.example.com/lib-1.2.3.tar.gz`。
    pub uri: String,
    pub digest: DigestSet,
}

/// 构建输出的标识与摘要。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Artifact {
    /// 构建产物逻辑名，例如 `bin/app-1.0.0`。
    pub name: String,
    pub digest: DigestSet,
}

/// 证明主体：源提交信息（SLSA 风格）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceCommit {
    /// 规范仓库地址（不含 `git+` 前缀），如 `https://example.com/org/repo`。
    pub repository: String,
    /// 提交哈希（十六进制字符串）。
    pub ref_commit: String,
}

/// 构建来源（build provenance）谓词主体。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BuildProvenance {
    /// 构建器标识，如 `pkg:generic/ci-builder@v2`。
    pub builder_id: String,
    /// 证明主体引用的源提交。
    pub source_commit: SourceCommit,
    /// 本次构建消费的全部材料（必须包含源提交对应的 git 材料）。
    pub materials: Vec<Material>,
    /// 构建产物及其摘要。
    pub output: Artifact,
    /// 构建完成时间（RFC3339），仅作记录，不参与策略判定。
    #[serde(default)]
    pub built_at: String,
}

/// in-toto 风格陈述：`payloadType` + JSON 编码的 `payload`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Statement {
    #[serde(rename = "_type")]
    pub statement_type: String,
    pub subject: Vec<Artifact>,
    pub predicate_type: String,
    pub predicate: BuildProvenance,
}

impl Statement {
    pub const STATEMENT_TYPE: &'static str = "https://in-toto.io/Statement/v1";
    pub const PREDICATE_TYPE: &'static str =
        "https://slsa.dev/provenance/v1/build-provenance-demo";

    /// 规范字节表示：紧凑（无空白）、按 key 排序的 JSON。
    /// 签名与验签双方必须使用同一表示。
    pub fn canonical_bytes(&self) -> anyhow::Result<Vec<u8>> {
        let value = serde_json::to_value(self)?;
        canonical_json(&value)
    }
}

/// 递归按 key 排序后紧凑序列化，得到与字段顺序无关的规范字节。
pub fn canonical_json(value: &serde_json::Value) -> anyhow::Result<Vec<u8>> {
    fn sort(value: &mut serde_json::Value) {
        match value {
            serde_json::Value::Object(map) => {
                let mut sorted: std::collections::BTreeMap<String, serde_json::Value> =
                    std::mem::take(map).into_iter().collect();
                for v in sorted.values_mut() {
                    sort(v);
                }
                *map = sorted.into_iter().collect();
            }
            serde_json::Value::Array(arr) => arr.iter_mut().for_each(sort),
            _ => {}
        }
    }
    let mut v = value.clone();
    sort(&mut v);
    Ok(serde_json::to_vec(&v)?)
}

/// 签名信封：DSSE 风格，对 PAE(payloadType, payload) 做 Ed25519 签名。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Signature {
    /// 签名者标识（构建器 id），用于在注册表中定位公钥。
    pub keyid: String,
    /// base64 编码的 Ed25519 签名。
    pub sig: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Envelope {
    pub payload_type: String,
    /// base64 编码的规范 JSON 陈述。
    pub payload: String,
    pub signatures: Vec<Signature>,
}

impl Envelope {
    pub const PAYLOAD_TYPE: &'static str =
        "application/vnd.in-toto+json; type=https://slsa.dev/provenance/v1";

    /// DSSE Pre-Authentication Encoding。
    pub fn pae(&self) -> anyhow::Result<Vec<u8>> {
        let payload_bytes =
            base64::engine::general_purpose::STANDARD.decode(&self.payload)?;
        let mut out = Vec::new();
        out.extend_from_slice(b"DSSEv1 ");
        out.extend_from_slice(self.payload_type.as_bytes());
        out.push(b' ');
        out.extend_from_slice(payload_bytes.len().to_string().as_bytes());
        out.push(b' ');
        out.extend_from_slice(&payload_bytes);
        Ok(out)
    }
}

/// POST /verify 请求体。
#[derive(Debug, Clone, Deserialize)]
pub struct VerifyRequest {
    /// 待验证的签名信封（二选一：或使用 `proof_path`）。
    #[serde(default)]
    pub envelope: Option<Envelope>,
    /// fixtures 目录下 proofs/ 内的相对路径（如 `valid.json`）。
    #[serde(default)]
    pub proof_path: Option<String>,
    /// 验证者实际拿到的构建产物原始字节（服务端据此计算摘要）。
    #[serde(default)]
    pub artifact_bytes: Option<String>,
    /// 验证者实际拿到产物的 base64 编码（与 `artifact_bytes` 二选一）。
    #[serde(default)]
    pub artifact_base64: Option<String>,
    /// 或直接给出验证者侧计算好的实际摘要十六进制。
    #[serde(default)]
    pub actual_output_digest: Option<String>,
    /// fixtures 目录下 artifacts/ 内的相对路径（如 `app-v1.0.0.tar.gz`），
    /// 服务端读取该夹具并计算实际摘要。
    #[serde(default)]
    pub artifact_path: Option<String>,
    /// 本次待准入构建实际执行构建的构建器 id（由准入环境如实提供）。
    /// 用于检测“真证明被跨构建器复用”：必须与证明中的 builder_id 一致。
    #[serde(default)]
    pub actual_builder_id: Option<String>,
}

/// 单条策略检查的判定。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CheckResult {
    /// 检查项标识，如 `signature`、`trusted_builder`。
    pub check: String,
    pub passed: bool,
    /// 机器可读的原因码，如 `OUTPUT_DIGEST_MISMATCH`。
    pub code: String,
    /// 人类可读的说明。
    pub detail: String,
}

/// 一次验证的完整报告。
#[derive(Debug, Clone, Serialize)]
pub struct VerificationReport {
    pub accepted: bool,
    pub checks: Vec<CheckResult>,
    /// 从信封中解析出的陈述（解析失败时为 None），便于调用方核对。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub statement: Option<Statement>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signer_keyid: Option<String>,
    /// 验证者侧实际输出摘要（hex），若提供。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub actual_output_digest: Option<String>,
}
