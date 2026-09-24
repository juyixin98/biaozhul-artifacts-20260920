//! 服务入口：`provenance-server [监听地址] [fixtures 目录]`。

use build_provenance_verify::load_fixtures;
use std::net::SocketAddr;
use std::path::PathBuf;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let addr: SocketAddr = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "127.0.0.1:8080".to_string())
        .parse()
        .map_err(|e| anyhow::anyhow!("无效监听地址: {e}"))?;

    let fixtures_dir: PathBuf = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "fixtures".to_string())
        .into();

    let verifier = load_fixtures(fixtures_dir)?;
    tracing::info!(
        "已加载夹具：{} 个可信构建器，{} 个已注册签名密钥，{} 份本地材料",
        verifier.policy.trusted_builders.len(),
        verifier.keys.registered_keyids().len(),
        verifier.materials.uris().len(),
    );

    build_provenance_verify::server::serve(verifier, addr).await
}
