//! 构建来源证明验证服务入口。
//!
//! 环境变量：
//! - `PROVENANCE_FIXTURES_DIR`：夹具目录（默认 `./fixtures`），缺失时自动生成；
//! - `PROVENANCE_LISTEN_ADDR`：监听地址（默认 `127.0.0.1:8080`）。

use std::sync::Arc;

use provenance::api;
use provenance::fixture;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = std::env::var("PROVENANCE_FIXTURES_DIR").unwrap_or_else(|_| "fixtures".to_string());
    let addr =
        std::env::var("PROVENANCE_LISTEN_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());

    let bundle = fixture::load_or_create(std::path::Path::new(&dir))?;
    println!(
        "已加载策略：{} 个可信构建器，{} 个允许源仓库，{} 个允许材料来源",
        bundle.policy.trusted_builders.len(),
        bundle.policy.allowed_source_repos.len(),
        bundle.policy.allowed_material_origins.len(),
    );
    println!("夹具目录: {}", dir);

    let app = api::router(Arc::new(bundle));
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    println!("构建证明验证服务监听: http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
