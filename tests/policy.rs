//! 策略逐条判定测试：三个验收场景 + 附加场景。

mod common;

use common::*;
use build_provenance_verify::VerifyRequest;
use serde_json::json;

#[test]
fn scenario_valid_is_accepted() {
    let app = TestApp::new();
    let req: VerifyRequest =
        serde_json::from_value(json!({
            "proof_path": "valid.json",
            "artifact_path": "app-v1.0.0.tar.gz",
            "actual_builder_id": TRUSTED
        }))
        .unwrap();
    let report = app.verify_direct(&req);
    assert!(report.accepted, "合法证明应当通过: {report:?}");
    assert_eq!(report.checks.len(), 8);
    assert!(report.checks.iter().all(|c| c.passed));
}

#[test]
fn scenario_output_replacement_is_detected() {
    let app = TestApp::new();
    // 证明真实有效，但验证者拿到的产物是被替换的版本。
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "output-replaced.json",
        "artifact_path": "app-v1.0.0-tampered.tar.gz",
        "actual_builder_id": TRUSTED
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);

    // 除输出摘要外的其余策略全部通过——证明替换只影响输出比对。
    for c in &report.checks {
        let expected = c.check != "output_digest";
        assert_eq!(c.passed, expected, "检查 {} 判定异常: {c:?}", c.check);
    }
    let output = report
        .checks
        .iter()
        .find(|c| c.check == "output_digest")
        .unwrap();
    assert_eq!(output.code, "OUTPUT_DIGEST_MISMATCH");
}

#[test]
fn scenario_missing_material_is_detected() {
    let app = TestApp::new();
    // 证明声明了本地材料库中不存在的 dep-b-0.9.5。
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "missing-material.json",
        "artifact_path": "app-v1.0.0.tar.gz",
        "actual_builder_id": TRUSTED
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);
    let materials = report
        .checks
        .iter()
        .find(|c| c.check == "materials_complete")
        .unwrap();
    assert!(!materials.passed);
    assert_eq!(materials.code, "MATERIAL_MISSING");
    assert!(materials.detail.contains("dep-b-0.9.5"));
}

#[test]
fn scenario_cross_builder_reuse_is_detected() {
    let app = TestApp::new();
    // 可信构建器 A 的真证明被拿去给影子构建器 B 的产物背书。
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "cross-builder-reuse.json",
        "artifact_path": "app-v1.0.0-from-shadow-builder.tar.gz",
        "actual_builder_id": ROGUE
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);

    let binding = report
        .checks
        .iter()
        .find(|c| c.check == "builder_binding")
        .unwrap();
    assert_eq!(binding.code, "CROSS_BUILDER_REUSE");
    assert!(binding.detail.contains("跨构建器复用"));

    // 由于两个构建器产物字节不同，输出摘要同样不匹配。
    let output = report
        .checks
        .iter()
        .find(|c| c.check == "output_digest")
        .unwrap();
    assert_eq!(output.code, "OUTPUT_DIGEST_MISMATCH");
}

#[test]
fn cross_builder_reuse_detected_even_with_identical_bytes() {
    // 关键安全属性：即使 B 逐字节复制 A 的产物（输出摘要相同），
    // 构建器身份绑定仍必须拒绝复用证明。
    let app = TestApp::new();
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "valid.json",
        "artifact_path": "app-v1.0.0.tar.gz", // 与证明完全一致的字节
        "actual_builder_id": ROGUE           // 但实际构建者是 B
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);
    let binding = report
        .checks
        .iter()
        .find(|c| c.check == "builder_binding")
        .unwrap();
    assert_eq!(binding.code, "CROSS_BUILDER_REUSE");
    let output = report
        .checks
        .iter()
        .find(|c| c.check == "output_digest")
        .unwrap();
    assert!(output.passed, "字节相同，输出摘要应通过");
}

#[test]
fn scenario_forbidden_source_is_detected() {
    let app = TestApp::new();
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "forbidden-source.json",
        "artifact_path": "app-v1.0.0.tar.gz",
        "actual_builder_id": TRUSTED
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);
    let sources = report
        .checks
        .iter()
        .find(|c| c.check == "material_sources_allowed")
        .unwrap();
    assert_eq!(sources.code, "MATERIAL_SOURCE_DENIED");
    assert!(sources.detail.contains("evil-mirror"));
    // 该材料在本地库中实际存在，因此“齐备性”通过、“来源”拒绝。
    assert!(report
        .checks
        .iter()
        .find(|c| c.check == "materials_complete")
        .unwrap()
        .passed);
}

#[test]
fn scenario_untrusted_builder_is_detected() {
    let app = TestApp::new();
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "untrusted-builder.json",
        "artifact_path": "app-v1.0.0.tar.gz",
        "actual_builder_id": ROGUE
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);
    let trusted = report
        .checks
        .iter()
        .find(|c| c.check == "trusted_builder")
        .unwrap();
    assert_eq!(trusted.code, "UNTRUSTED_BUILDER");
    // 签名本身合法（影子构建器确实持有对应私钥）。
    assert!(report
        .checks
        .iter()
        .find(|c| c.check == "signature")
        .unwrap()
        .passed);
}

#[test]
fn missing_artifact_input_fails_output_check_only() {
    let app = TestApp::new();
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "valid.json",
        "actual_builder_id": TRUSTED
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    assert!(!report.accepted);
    let output = report
        .checks
        .iter()
        .find(|c| c.check == "output_digest")
        .unwrap();
    assert_eq!(output.code, "ARTIFACT_NOT_SUPPLIED");
}

#[test]
fn tampered_material_is_rejected() {
    // 临时改坏本地材料文件：齐备性检查应以 MATERIAL_DIGEST_MISMATCH 失败。
    let app = TestApp::new();
    let dep = app.dir.path().join("materials/deps/dep-a-2.1.0.tar.gz");
    std::fs::write(dep, b"corrupted content").unwrap();
    let req: VerifyRequest = serde_json::from_value(json!({
        "proof_path": "valid.json",
        "artifact_path": "app-v1.0.0.tar.gz",
        "actual_builder_id": TRUSTED
    }))
    .unwrap();
    let report = app.verify_direct(&req);
    let materials = report
        .checks
        .iter()
        .find(|c| c.check == "materials_complete")
        .unwrap();
    assert_eq!(materials.code, "MATERIAL_DIGEST_MISMATCH");
}
