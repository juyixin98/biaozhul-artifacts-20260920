//! 确定性生成全部本地夹具：构建器密钥对、材料、构建产物、
//! 签名证明信封与 HTTP 请求样例。
//!
//! 用法：`cargo run --bin gen-fixtures [fixtures 目录]`
//!
//! 密钥由固定种子派生（Ed25519），因此每次重新生成结果一致；
//! 这*只适用于夹具/测试*，生产环境应使用随机种子并妥善保管私钥。

use build_provenance_verify::crypto::{sha256, SignerIdentity};
use build_provenance_verify::types::{
    Artifact, BuildProvenance, DigestSet, Material, SourceCommit, Statement,
};
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

const TRUSTED_BUILDER: &str = "pkg:generic/ci-builder@v2";
const ROGUE_BUILDER: &str = "pkg:generic/shadow-builder@v9";
const REPO: &str = "https://vcs.example.com/team/service";
const DEPS_PREFIX: &str = "https://deps.example.com/";
const EVIL_PREFIX: &str = "https://evil-mirror.example.test/";

fn seed(label: &[u8]) -> [u8; 32] {
    let mut s = [0u8; 32];
    let d = sha256(label);
    s.copy_from_slice(&d);
    s
}

fn write_json(path: &Path, value: &serde_json::Value) {
    let pretty = serde_json::to_vec_pretty(value).expect("serialize");
    fs::write(path, pretty).expect("write file");
}

fn git_material_uri(subpath: &str) -> String {
    // 源材料 URI：git+<repo>@<commit>/<subpath>
    format!("git+{REPO}@{COMMIT}/{subpath}")
}

const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

fn main() {
    let dir_arg = std::env::args().nth(1).unwrap_or_else(|| "fixtures".to_string());
    let root = Path::new(&dir_arg);
    for sub in ["materials/src", "materials/deps", "artifacts", "proofs", "requests"] {
        fs::create_dir_all(root.join(sub)).expect("mkdir");
    }

    // ---- 身份：可信构建器 + 影子构建器（两把不同的确定性密钥）----
    let trusted = SignerIdentity::from_seed(TRUSTED_BUILDER, seed(b"trusted-ci-builder-v2"));
    let rogue = SignerIdentity::from_seed(ROGUE_BUILDER, seed(b"rogue-shadow-builder-v9"));

    // ---- 材料（全部本地）----
    let mut manifest: BTreeMap<String, String> = BTreeMap::new();

    let main_rs = b"fn main() { println!(\"service v1.0.0\"); }\n";
    let lib_rs = b"pub fn ping() -> &'static str { \"pong\" }\n";
    let dep_a = b"dep-a 2.1.0 source tarball payload\n";
    let dep_b = b"dep-b 0.9.4 source tarball payload\n";

    let files: [(&str, &[u8]); 4] = [
        ("materials/src/main.rs", main_rs),
        ("materials/src/lib.rs", lib_rs),
        ("materials/deps/dep-a-2.1.0.tar.gz", dep_a),
        ("materials/deps/dep-b-0.9.4.tar.gz", dep_b),
    ];
    for (rel, bytes) in files {
        fs::write(root.join(rel), bytes).expect("write material");
    }

    let src_main_uri = git_material_uri("src/main.rs");
    let src_lib_uri = git_material_uri("src/lib.rs");
    let dep_a_uri = format!("{DEPS_PREFIX}dep-a-2.1.0.tar.gz");
    let dep_b_uri = format!("{DEPS_PREFIX}dep-b-0.9.4.tar.gz");

    manifest.insert(src_main_uri.clone(), "src/main.rs".to_string());
    manifest.insert(src_lib_uri.clone(), "src/lib.rs".to_string());
    manifest.insert(dep_a_uri.clone(), "deps/dep-a-2.1.0.tar.gz".to_string());
    manifest.insert(dep_b_uri.clone(), "deps/dep-b-0.9.4.tar.gz".to_string());

    // 禁止来源的材料在本地库中也有一份副本（映射到 dep-b 字节），
    // 用于演示“材料齐备但来源被策略拒绝”。
    let evil_dep_uri = format!("{EVIL_PREFIX}dep-b-0.9.4.tar.gz");
    manifest.insert(evil_dep_uri.clone(), "deps/dep-b-0.9.4.tar.gz".to_string());

    write_json(
        &root.join("materials").join("MANIFEST.json"),
        &serde_json::to_value(&manifest).unwrap(),
    );

    let m = |uri: String, bytes: &[u8]| Material {
        uri,
        digest: DigestSet::new(sha256(bytes)),
    };
    let all_allowed_materials = vec![
        m(src_main_uri.clone(), main_rs),
        m(src_lib_uri, lib_rs),
        m(dep_a_uri, dep_a),
        m(dep_b_uri.clone(), dep_b),
    ];

    // ---- 构建产物 ----
    let good_artifact_bytes = b"==== service v1.0.0 release artifact (built by ci-builder) ====\n";
    let tampered_bytes = b"==== service v1.0.0 release artifact BACKDORED ====\n";
    let builder_b_bytes = b"==== service v1.0.0 release artifact (built by shadow-builder) ====\n";
    fs::write(root.join("artifacts/app-v1.0.0.tar.gz"), good_artifact_bytes).unwrap();
    fs::write(root.join("artifacts/app-v1.0.0-tampered.tar.gz"), tampered_bytes).unwrap();
    fs::write(root.join("artifacts/app-v1.0.0-from-shadow-builder.tar.gz"), builder_b_bytes).unwrap();

    let good_artifact = Artifact {
        name: "bin/app-v1.0.0.tar.gz".to_string(),
        digest: DigestSet::new(sha256(good_artifact_bytes)),
    };

    // ---- 陈述 / 证明 ----
    fn statement_with(materials: Vec<Material>, output: Artifact) -> Statement {
        Statement {
            statement_type: Statement::STATEMENT_TYPE.to_string(),
            subject: vec![output.clone()],
            predicate_type: Statement::PREDICATE_TYPE.to_string(),
            predicate: BuildProvenance {
                builder_id: TRUSTED_BUILDER.to_string(),
                source_commit: SourceCommit {
                    repository: REPO.to_string(),
                    ref_commit: COMMIT.to_string(),
                },
                materials,
                output,
                built_at: "2026-09-24T08:00:00Z".to_string(),
            },
        }
    }

    // valid：完整材料、合法签名、输出与证明一致。
    let st_valid = statement_with(all_allowed_materials.clone(), good_artifact.clone());
    let env_valid = trusted.sign_statement(&st_valid).unwrap();
    write_json(&root.join("proofs/valid.json"), &serde_json::to_value(&env_valid).unwrap());

    // output-replaced：同一份合法证明，但实际产物被换成带后门版本。
    write_json(
        &root.join("proofs/output-replaced.json"),
        &serde_json::to_value(&env_valid).unwrap(),
    );

    // missing-material：证明缺少一份依赖材料（dep-b），本地库里其实有，
    // 但证明未声明它 —— 材料齐备性检查同时检查“声明材料可核验”。
    // 为更贴合“缺失材料”语义，改为声明 dep-b 但指向一个本地库中不存在的 URI
    // （模拟验证者无法取得该材料）。
    let mut missing_materials = all_allowed_materials.clone();
    // 把 dep-b 的 URI 改为本地库中不存在的另一版本。
    if let Some(entry) = missing_materials.iter_mut().find(|x| x.uri == dep_b_uri) {
        entry.uri = format!("{DEPS_PREFIX}dep-b-0.9.5.tar.gz");
    }
    let st_missing = statement_with(missing_materials, good_artifact.clone());
    let env_missing = trusted.sign_statement(&st_missing).unwrap();
    write_json(
        &root.join("proofs/missing-material.json"),
        &serde_json::to_value(&env_missing).unwrap(),
    );

    // cross-builder-reuse：可信构建器为 v1.0.0 出具的真证明，
    // 被拿去给影子构建器实际产出的产物背书。
    write_json(
        &root.join("proofs/cross-builder-reuse.json"),
        &serde_json::to_value(&env_valid).unwrap(),
    );

    // forbidden-source（附加场景）：材料来自白名单外的镜像。
    let mut evil_materials = all_allowed_materials.clone();
    if let Some(entry) = evil_materials.iter_mut().find(|x| x.uri == dep_b_uri) {
        entry.uri = evil_dep_uri.clone();
    }
    let st_evil = statement_with(evil_materials, good_artifact.clone());
    let env_evil = trusted.sign_statement(&st_evil).unwrap();
    write_json(
        &root.join("proofs/forbidden-source.json"),
        &serde_json::to_value(&env_evil).unwrap(),
    );

    // untrusted-builder（附加场景）：影子构建器用自己的密钥自签。
    let mut st_rogue = st_valid.clone();
    st_rogue.predicate.builder_id = ROGUE_BUILDER.to_string();
    let env_rogue = rogue.sign_statement(&st_rogue).unwrap();
    write_json(
        &root.join("proofs/untrusted-builder.json"),
        &serde_json::to_value(&env_rogue).unwrap(),
    );

    // ---- 配置：策略 + 公钥注册表 ----
    let config = json!({
        "trusted_builders": [TRUSTED_BUILDER],
        "allowed_material_prefixes": [
            format!("git+{REPO}@"),
            DEPS_PREFIX,
        ],
        "allowed_source_repositories": [REPO],
        "keys": {
            TRUSTED_BUILDER: hex::encode(trusted.public_key_bytes()),
            ROGUE_BUILDER: hex::encode(rogue.public_key_bytes()),
        }
    });
    write_json(&root.join("config.json"), &config);

    // ---- HTTP 请求样例 ----
    let request = |proof: &str, artifact: &str, actual_builder: Option<&str>| {
        let mut v = json!({
            "proof_path": proof,
            "artifact_path": artifact,
        });
        if let Some(b) = actual_builder {
            v.as_object_mut()
                .unwrap()
                .insert("actual_builder_id".to_string(), json!(b));
        }
        v
    };

    let scenarios: [(&str, serde_json::Value); 6] = [
        (
            "01-valid",
            request("valid.json", "app-v1.0.0.tar.gz", Some(TRUSTED_BUILDER)),
        ),
        (
            "02-output-replaced",
            request(
                "output-replaced.json",
                "app-v1.0.0-tampered.tar.gz",
                Some(TRUSTED_BUILDER),
            ),
        ),
        (
            "03-missing-material",
            request(
                "missing-material.json",
                "app-v1.0.0.tar.gz",
                Some(TRUSTED_BUILDER),
            ),
        ),
        (
            "04-cross-builder-reuse",
            request(
                "cross-builder-reuse.json",
                "app-v1.0.0-from-shadow-builder.tar.gz",
                Some(ROGUE_BUILDER),
            ),
        ),
        (
            "05-forbidden-source",
            request("forbidden-source.json", "app-v1.0.0.tar.gz", Some(TRUSTED_BUILDER)),
        ),
        (
            "06-untrusted-builder",
            request(
                "untrusted-builder.json",
                "app-v1.0.0.tar.gz",
                Some(ROGUE_BUILDER),
            ),
        ),
    ];
    for (name, value) in scenarios {
        write_json(&root.join("requests").join(format!("{name}.json")), &value);
    }

    println!("fixtures generated under {}", root.display());
}
