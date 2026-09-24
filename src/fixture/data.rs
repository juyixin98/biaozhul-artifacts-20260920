//! 夹具常量：源提交、材料内容、构建器标识。
//!
//! 所有材料“内容”都是内联的固定字节，摘要由 [`crate::crypto::sha256_hex`]
//! 现算，保证夹具确定性且不依赖网络。

use crate::crypto::sha256_hex;
use crate::model::{Digest, Material, Output, SourceCommit, Statement};

pub const TRUSTED_BUILDER_A: &str = "trusted-builder-a";
pub const ROGUE_BUILDER_R: &str = "rogue-builder-r";

pub const ALLOWED_REPO: &str = "https://git.example.com/app/payments";
pub const ROGUE_REPO: &str = "https://git.example.net/mirror/payments";
pub const ALLOWED_ORIGIN_DEPS: &str = "https://deps.example.com/distfiles/";
pub const ALLOWED_ORIGIN_VENDOR: &str = "https://vendor.example.com/releases/";
pub const ROGUE_ORIGIN: &str = "https://evil-cache.example.net/packages/";

/// 确定性“源提交”（夹具中的 git 哈希）。
pub const SAMPLE_REVISION: &str = "7b3f9c1e5a8d2f6041c9e7b6a3d8f0251c4e9b7a";

/// 用于跨构建器场景的另一提交（同一仓库）。
pub const OTHER_REVISION: &str = "1111222233334444555566667777888899990000";

pub struct MaterialSpec {
    pub uri: &'static str,
    pub content: &'static [u8],
}

/// 合法构建声明包含的四项材料。
pub fn sample_material_specs() -> Vec<MaterialSpec> {
    vec![
        MaterialSpec {
            uri: "https://deps.example.com/distfiles/serde-1.0.tar.gz",
            content: b"fixture material: serde 1.0 distfile bytes",
        },
        MaterialSpec {
            uri: "https://deps.example.com/distfiles/tokio-1.43.tar.gz",
            content: b"fixture material: tokio 1.43 distfile bytes",
        },
        MaterialSpec {
            uri: "https://vendor.example.com/releases/acme-crypto-2.1.bin",
            content: b"fixture material: acme-crypto 2.1 vendor release",
        },
        MaterialSpec {
            uri: "https://vendor.example.com/releases/acme-sdk-5.0.bin",
            content: b"fixture material: acme-sdk 5.0 vendor release",
        },
    ]
}

/// 跨构建器场景中 rogue 构建器拉取的恶意缓存材料。
pub const ROGUE_MATERIAL_URI: &str = "https://evil-cache.example.net/packages/acme-crypto-2.1.bin";
pub const ROGUE_MATERIAL_CONTENT: &[u8] =
    b"fixture material: BACKDOORED acme-crypto 2.1 from evil cache";

/// 合法构建产生的输出内容与被替换的输出内容。
pub const GOOD_OUTPUT_CONTENT: &[u8] =
    b"fixture build output: payments-service v1.4.0 (legit build)";
pub const EVIL_OUTPUT_CONTENT: &[u8] =
    b"fixture build output: payments-service v1.4.0 (TAMPERED build)";

pub fn digest_of(content: &[u8]) -> Digest {
    Digest {
        alg: "sha256".to_string(),
        hex: sha256_hex(content),
    }
}

pub fn material(spec: &MaterialSpec) -> Material {
    Material {
        uri: spec.uri.to_string(),
        digest: digest_of(spec.content),
    }
}

pub fn sample_materials() -> Vec<Material> {
    sample_material_specs().iter().map(material).collect()
}

pub fn source_commit(repo: &str, revision: &str) -> SourceCommit {
    SourceCommit {
        repo: repo.to_string(),
        revision: revision.to_string(),
    }
}

pub fn sample_source_commit() -> SourceCommit {
    source_commit(ALLOWED_REPO, SAMPLE_REVISION)
}

pub fn output_of(content: &[u8]) -> Output {
    Output {
        digest: digest_of(content),
    }
}

/// 合法构建器对合法源提交构建出的证明主体。
pub fn sample_statement() -> Statement {
    Statement {
        builder_id: TRUSTED_BUILDER_A.to_string(),
        source_commit: sample_source_commit(),
        materials: sample_materials(),
        output: output_of(GOOD_OUTPUT_CONTENT),
    }
}
