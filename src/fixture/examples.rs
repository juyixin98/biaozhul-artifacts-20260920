//! 四类验收场景的示例请求（由 `gen-fixtures` 序列化为 `fixtures/examples/*.json`）。
//!
//! 每个场景都刻意设计成只触发对应的策略判定，便于在演示与测试中隔离归因。

use crate::fixture::data;
use crate::fixture::{seal, FixtureBundle};
use crate::model::{Material, SourceCommit, Statement};
use crate::verify::VerifyRequest;

pub const VALID: &str = "01_valid";
pub const OUTPUT_SUBSTITUTED: &str = "02_output_substituted";
pub const MATERIAL_MISSING: &str = "03_material_missing";
pub const CROSS_BUILDER: &str = "04_cross_builder_reuse";

pub fn all(bundle: &FixtureBundle) -> Vec<(&'static str, VerifyRequest)> {
    vec![
        (VALID, valid(bundle)),
        (OUTPUT_SUBSTITUTED, output_substituted(bundle)),
        (MATERIAL_MISSING, material_missing(bundle)),
        (CROSS_BUILDER, cross_builder_reuse(bundle)),
    ]
}

/// 场景 0（基线）：合法构建器、全部材料登记且摘要一致、输出未被替换。
pub fn valid(bundle: &FixtureBundle) -> VerifyRequest {
    let st = data::sample_statement();
    VerifyRequest {
        envelope: seal(bundle, data::TRUSTED_BUILDER_A, st),
        expected_source_commit: data::sample_source_commit(),
        expected_materials: data::sample_materials(),
        expected_output: Some(data::output_of(data::GOOD_OUTPUT_CONTENT)),
    }
}

/// 场景 1（输出替换）：证明本身合法且签名有效，但消费者实际拿到的制品
/// 已被换成另一份内容 —— 由 `output_match` 策略判定拒绝。
pub fn output_substituted(bundle: &FixtureBundle) -> VerifyRequest {
    let mut req = valid(bundle);
    req.expected_output = Some(data::output_of(data::EVIL_OUTPUT_CONTENT));
    req
}

/// 场景 2（缺失材料）：证明由合法构建器签署、签名有效，但证明只登记了
/// 3 项材料，漏掉消费者期望中的 `acme-sdk` —— 由 `materials_complete` 拒绝。
pub fn material_missing(bundle: &FixtureBundle) -> VerifyRequest {
    let all_specs = data::sample_material_specs();
    // 只保留前三项（漏掉 acme-sdk-5.0）。
    let partial: Vec<Material> = all_specs.iter().take(3).map(data::material).collect();

    let st = Statement {
        builder_id: data::TRUSTED_BUILDER_A.to_string(),
        source_commit: data::sample_source_commit(),
        materials: partial,
        output: data::output_of(data::GOOD_OUTPUT_CONTENT),
    };

    VerifyRequest {
        envelope: seal(bundle, data::TRUSTED_BUILDER_A, st),
        expected_source_commit: data::sample_source_commit(),
        expected_materials: data::sample_materials(),
        expected_output: Some(data::output_of(data::GOOD_OUTPUT_CONTENT)),
    }
}

/// 场景 3（跨构建器复用证明）：证明主体内容与合法场景逐字节相同，
/// 但被 rogue 构建器用自己的密钥重新签名（key_id 换成 rogue 公钥）。
/// 签名自洽，但信封公钥与构建器 A 的注册公钥不符 —— 由
/// `signing_key_binding` 策略判定拒绝，证明不能在构建器之间复用。
pub fn cross_builder_reuse(bundle: &FixtureBundle) -> VerifyRequest {
    let st = data::sample_statement();
    VerifyRequest {
        envelope: seal(bundle, data::ROGUE_BUILDER_R, st),
        expected_source_commit: data::sample_source_commit(),
        expected_materials: data::sample_materials(),
        expected_output: Some(data::output_of(data::GOOD_OUTPUT_CONTENT)),
    }
}

/// 测试辅助：构造 rogue 构建器按自身身份构建（builder_id 也是 rogue，
/// 材料来自恶意缓存、输出被植入后门）的请求，用于覆盖其余策略。
pub fn rogue_build(bundle: &FixtureBundle) -> VerifyRequest {
    let st = Statement {
        builder_id: data::ROGUE_BUILDER_R.to_string(),
        source_commit: SourceCommit {
            repo: data::ALLOWED_REPO.to_string(),
            revision: data::SAMPLE_REVISION.to_string(),
        },
        materials: vec![
            data::material(&data::sample_material_specs()[0]),
            Material {
                uri: data::ROGUE_MATERIAL_URI.to_string(),
                digest: data::digest_of(data::ROGUE_MATERIAL_CONTENT),
            },
        ],
        output: data::output_of(data::EVIL_OUTPUT_CONTENT),
    };
    VerifyRequest {
        envelope: seal(bundle, data::ROGUE_BUILDER_R, st),
        expected_source_commit: data::sample_source_commit(),
        expected_materials: data::sample_materials(),
        expected_output: Some(data::output_of(data::EVIL_OUTPUT_CONTENT)),
    }
}
