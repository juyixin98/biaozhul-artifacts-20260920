//! 集成测试公共辅助：生成临时夹具、构建内存服务。
#![allow(dead_code)]

use build_provenance_verify::server::{router, AppState};
use build_provenance_verify::{load_fixtures, VerifyRequest};
use std::path::PathBuf;
use std::sync::Arc;
use tempfile::TempDir;
use tower::ServiceExt;

pub const TRUSTED: &str = "pkg:generic/ci-builder@v2";
pub const ROGUE: &str = "pkg:generic/shadow-builder@v9";

/// 运行 `gen-fixtures` 等价逻辑：直接调用仓库内已提交的 fixtures 更稳定，
/// 但为保证测试独立，复制仓库 `fixtures/` 到临时目录。
pub fn copy_fixtures() -> TempDir {
    let tmp = TempDir::new().unwrap();
    let src = fixture_root();
    copy_dir(&src, tmp.path()).unwrap();
    tmp
}

fn fixture_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures")
}

fn copy_dir(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let to = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &to)?;
        } else {
            std::fs::copy(entry.path(), to)?;
        }
    }
    Ok(())
}

pub struct TestApp {
    pub dir: TempDir,
}

impl TestApp {
    pub fn new() -> Self {
        TestApp {
            dir: copy_fixtures(),
        }
    }

    pub fn app(&self) -> axum::Router {
        let verifier = load_fixtures(self.dir.path().to_path_buf()).unwrap();
        router(AppState {
            verifier: Arc::new(verifier),
        })
    }

    /// 直接走策略引擎（不经过 HTTP 层）。
    pub fn verify_direct(&self, req: &VerifyRequest) -> build_provenance_verify::VerificationReport {
        let verifier = load_fixtures(self.dir.path().to_path_buf()).unwrap();
        let mut req = req.clone();
        // 与 HTTP handler 的 resolve_artifact_path 等价：读取夹具产物并计算摘要。
        if let Some(rel) = req.artifact_path.take() {
            let bytes = std::fs::read(self.dir.path().join("artifacts").join(rel)).unwrap();
            req.actual_output_digest =
                Some(hex::encode(build_provenance_verify::sha256(&bytes)));
        }
        let envelope = match (&req.envelope, &req.proof_path) {
            (Some(e), _) => e.clone(),
            (_, Some(rel)) => verifier.load_proof_envelope(rel).unwrap(),
            _ => panic!("请求必须包含 envelope 或 proof_path"),
        };
        verifier.verify(&envelope, &req)
    }

    /// 发起 POST /verify 请求，返回 (状态码, JSON)。
    pub async fn post_verify(
        &self,
        body: serde_json::Value,
    ) -> (u16, serde_json::Value) {
        use axum::body::Body;
        use axum::http::Request;
        let resp = self
            .app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/verify")
                    .header("content-type", "application/json")
                    .body(Body::from(serde_json::to_vec(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status().as_u16();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        (status, value)
    }

    /// 发起 POST /scenarios/{name} 请求。
    pub async fn run_scenario(&self, name: &str) -> (u16, serde_json::Value) {
        use axum::body::Body;
        use axum::http::Request;
        let resp = self
            .app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/scenarios/{name}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status().as_u16();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        (status, value)
    }
}

/// 读取夹具文件原始字节。
pub fn read_fixture(rel: &str) -> Vec<u8> {
    std::fs::read(fixture_root().join(rel)).unwrap()
}

/// 从报告中取某条检查。
pub fn check<'a>(report: &'a serde_json::Value, name: &str) -> &'a serde_json::Value {
    report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["check"] == name)
        .unwrap_or_else(|| panic!("缺少检查项 {name}"))
}
