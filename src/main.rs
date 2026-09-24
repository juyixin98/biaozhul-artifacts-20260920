use std::net::SocketAddr;
use std::path::Path;
use std::sync::Arc;

use seglog::api::{router, AppState};
use seglog::store::Store;

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

#[tokio::main]
async fn main() {
    seglog::crash::init_from_env();

    let addr: SocketAddr = env_or("SEGLOG_ADDR", "127.0.0.1:3000")
        .parse()
        .expect("SEGLOG_ADDR 不是合法的监听地址");
    let data_dir = env_or("SEGLOG_DATA_DIR", "./data");
    let max_segment: u64 = env_or("SEGLOG_MAX_SEGMENT", "1048576")
        .parse()
        .expect("SEGLOG_MAX_SEGMENT 不是合法数字");

    // 启动即扫描并恢复所有日志;任何损坏都会导致拒绝启动。
    let store = match Store::open(Path::new(&data_dir), max_segment) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[seglog] 拒绝启动: {e}");
            std::process::exit(1);
        }
    };

    let app = router(AppState { store: Arc::new(store) });
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("绑定监听地址失败");
    eprintln!("[seglog] 监听 http://{addr}  数据目录 {data_dir}  段上限 {max_segment} 字节");
    axum::serve(listener, app).await.expect("HTTP 服务异常退出");
}
