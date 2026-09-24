//! 端到端演示（不依赖 HTTP 客户端库，用 tokio TCP 手写最小 HTTP/1.1 请求）。
//!
//! 运行前先启动服务：
//!
//! ```bash
//! cargo run --bin rbc-server -- --store ./demo-cache
//! cargo run --example demo                     # 默认 127.0.0.1:8080
//! cargo run --example demo -- 127.0.0.1:9000
//! ```
//!
//! 演示脚本会依次展示：
//! 1. 未命中（MISS，需要重新执行）
//! 2. 重新执行：上传输出块 → 发布动作结果（全量核验后才落盘）
//! 3. 再次查询：命中（HIT，且命中前重新核验全部对象）
//! 4. 缺块发布被拒（424，不产生记录）
//! 5. 提示如何模拟磁盘损坏后得到 503（脚本自身不篡改磁盘）

use std::collections::BTreeMap;

use remote_build_cache::hash::digest_bytes;
use remote_build_cache::models::{
    Action, ActionResult, Digest, OutputFile, PublishActionRequest,
};

#[tokio::main]
async fn main() {
    let addr = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "127.0.0.1:8080".to_string());

    println!("# 远端构建缓存原型演示 -> http://{addr}\n");

    // 1) 构造动作并请求规范摘要
    let mut platform = BTreeMap::new();
    platform.insert("os".to_string(), "linux".to_string());
    platform.insert("cpu".to_string(), "x86_64".to_string());
    let action = Action {
        arguments: vec!["gcc".into(), "-c".into(), "main.c".into(), "-o".into(), "main.o".into()],
        input_root_digest: None,
        platform,
        environment_variables: BTreeMap::new(),
        working_directory: ".".into(),
    };
    let action_json = serde_json::to_vec(&action).unwrap();
    let resp = http(
        &addr,
        "POST",
        "/util/action-digest",
        Some(("application/json", &action_json)),
    )
    .await;
    assert!(resp.status == 200, "action digest failed: {:?}", resp);
    let meta: serde_json::Value = serde_json::from_slice(&resp.body).unwrap();
    let action_hash = meta["hash"].as_str().unwrap().to_string();
    println!("[1] 动作摘要 (SHA-256) = {action_hash}");

    // 2) 发布前查询 → 缓存未命中
    let action_uri = format!("/actions/{action_hash}");
    let resp = http(&addr, "GET", &action_uri, None).await;
    println!("[2] 发布前 GET {action_uri}");
    println!("    -> {} {}\n", resp.status, resp.text().lines().next().unwrap_or(""));
    assert_eq!(resp.status, 404);

    // 3) “重新执行”：上传产物块
    let artifact = b"<<< fake ELF main.o bytes >>>".to_vec();
    let blob_digest = Digest {
        hash: digest_bytes(&artifact),
        size_bytes: artifact.len() as u64,
    };
    let put_uri = format!(
        "/blobs/{}?size_bytes={}",
        blob_digest.hash, blob_digest.size_bytes
    );
    let resp = http(
        &addr,
        "PUT",
        &put_uri,
        Some(("application/octet-stream", &artifact)),
    )
    .await;
    println!("[3] 重新执行后上传产物块 PUT {put_uri}");
    println!("    -> {} {}\n", resp.status, resp.text().lines().next().unwrap_or(""));
    assert_eq!(resp.status, 200);

    // 4) 发布动作结果（全部对象核验通过才会落盘）
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: Some("build ok\n".into()),
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "main.o".into(),
            digest: blob_digest.clone(),
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let publish_body = serde_json::to_vec(&PublishActionRequest {
        action: action.clone(),
        action_result: result,
    })
    .unwrap();
    let resp = http(
        &addr,
        "PUT",
        &action_uri,
        Some(("application/json", &publish_body)),
    )
    .await;
    println!("[4] 发布动作结果 PUT {action_uri}（服务端逐个核验引用对象）");
    println!("    -> {} {}\n", resp.status, resp.text().lines().next().unwrap_or(""));
    assert_eq!(resp.status, 200);

    // 5) 再次查询 → 命中
    let resp = http(&addr, "GET", &action_uri, None).await;
    println!("[5] 再次 GET {action_uri}");
    println!("    -> {} {}\n", resp.status, resp.text().lines().next().unwrap_or(""));
    assert_eq!(resp.status, 200);

    // 6) 缺块发布必须被拒
    let bad_action = Action {
        arguments: vec!["never".into()],
        input_root_digest: None,
        platform: BTreeMap::new(),
        environment_variables: BTreeMap::new(),
        working_directory: ".".into(),
    };
    let ghost = Digest {
        hash: digest_bytes(b"ghost"),
        size_bytes: 5,
    };
    let bad = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "ghost".into(),
            digest: ghost,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let bad_action_json = serde_json::to_vec(&bad_action).unwrap();
    // 让服务端计算该 action 的真实摘要作为 URL 键
    let meta_resp = http(
        &addr,
        "POST",
        "/util/action-digest",
        Some(("application/json", &bad_action_json)),
    )
    .await;
    let bad_hash = serde_json::from_slice::<serde_json::Value>(&meta_resp.body).unwrap()
        ["hash"]
        .as_str()
        .unwrap()
        .to_string();
    let bad_body = serde_json::to_vec(&PublishActionRequest {
        action: bad_action,
        action_result: bad,
    })
    .unwrap();
    let resp = http(
        &addr,
        "PUT",
        &format!("/actions/{bad_hash}"),
        Some(("application/json", &bad_body)),
    )
    .await;
    println!("[6] 缺块发布（引用从未上传的对象）");
    println!("    -> {} {}\n", resp.status, resp.text().lines().next().unwrap_or(""));
    assert_eq!(resp.status, 424);

    println!("全部预期断言通过。");
    println!("\n手动验证损坏场景：");
    println!("  # 篡改某个 CAS 文件后再查询，应得到 503 cache_corruption，而非伪成功：");
    println!("  f=$(find ./demo-cache/cas -type f | head -1)");
    println!("  echo corrupted | sudo tee \"$f\"  # 或直接覆盖写入");
    println!("  curl -sS http://{addr}{action_uri}");
}

// ---------------- 极简 HTTP/1.1 客户端 ----------------

#[derive(Debug)]
struct HttpResponse {
    status: u16,
    body: Vec<u8>,
}

impl HttpResponse {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).to_string()
    }
}

async fn http(
    addr: &str,
    method: &str,
    uri: &str,
    body: Option<(&str, &[u8])>,
) -> HttpResponse {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    let mut stream = TcpStream::connect(addr).await.expect("connect to server");
    let mut head = format!("{method} {uri} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n");
    if let Some((ct, data)) = &body {
        head.push_str(&format!("Content-Type: {ct}\r\n"));
        head.push_str(&format!("Content-Length: {}\r\n", data.len()));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).await.unwrap();
    if let Some((_, data)) = &body {
        stream.write_all(data).await.unwrap();
    }
    stream.flush().await.unwrap();

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).await.unwrap();

    // 解析状态行 + 头，按 Content-Length 取 body
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .expect("http headers");
    let header_text = String::from_utf8_lossy(&raw[..split]);
    let mut lines = header_text.split("\r\n");
    let status = lines
        .next()
        .unwrap()
        .split(' ')
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let mut content_length = None;
    for line in lines {
        if let Some(rest) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            content_length = Some(rest.trim().parse::<usize>().unwrap());
        }
    }
    let body_start = split + 4;
    let body = match content_length {
        Some(n) => raw[body_start..body_start + n].to_vec(),
        None => raw[body_start..].to_vec(),
    };
    HttpResponse { status, body }
}
