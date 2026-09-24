//! 集成测试公共设施：父进程绑定监听 socket 并经 fd=3 传给真实服务子进程，
//! 使用真实 HMAC-SHA256 签名与真实 SQLite 文件，杜绝并行端口竞态。
#![allow(dead_code, clippy::too_many_arguments)]

use std::net::TcpListener;
use std::os::fd::AsRawFd;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

pub const KEY_AU: &str = "test-autonomous-secret";
pub const KEY_RC: &str = "test-remote-secret";
pub const KEY_ES: &str = "test-estop-secret";
pub const ADMIN_TOKEN: &str = "test-admin-token";

pub struct Keys {
    pub autonomous: Vec<u8>,
    pub remote: Vec<u8>,
    pub estop: Vec<u8>,
}

pub struct Env {
    pub child: Child,
    pub base: String,
    pub keys: Keys,
    pub client: reqwest::blocking::Client,
    /// 保持父进程端监听 socket 存活（子进程继承同一 fd）。
    _listener: TcpListener,
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// 通过 fcntl 清掉继承 fd 的 CLOEXEC（不引入 libc 依赖）。
extern "C" {
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
}
const F_GETFD: i32 = 1;
const F_SETFD: i32 = 2;
const FD_CLOEXEC: i32 = 1;

fn clear_cloexec(fd: i32) {
    let flags = unsafe { fcntl(fd, F_GETFD) };
    assert!(flags >= 0, "F_GETFD failed");
    let r = unsafe { fcntl(fd, F_SETFD, flags & !FD_CLOEXEC) };
    assert!(r >= 0, "F_SETFD failed");
}

fn wait_healthy(base: &str) {
    let deadline = SystemTime::now() + Duration::from_secs(15);
    loop {
        if let Ok(resp) = reqwest::blocking::get(format!("{base}/health")) {
            if resp.status().is_success() {
                return;
            }
        }
        if SystemTime::now() > deadline {
            panic!("server at {base} did not become healthy");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// 启动一个服务实例。db_path=None 使用临时唯一数据库；Some(path) 用于重启测试。
pub fn setup(db_path: Option<&str>) -> Env {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let base = format!("http://127.0.0.1:{port}");

    let owned_db_dir;
    let db_file = match db_path {
        Some(p) => p.to_string(),
        None => {
            owned_db_dir = std::env::temp_dir().join(format!(
                "motion-arbiter-test-{}-{port}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir_all(&owned_db_dir).unwrap();
            owned_db_dir.join("arbiter.sqlite").to_string_lossy().to_string()
        }
    };

    clear_cloexec(listener.as_raw_fd());

    let mut cmd = Command::new(env!("CARGO_BIN_EXE_motion-arbiter"));
    cmd.env("ARBITER_CLOCK", "sim")
        .env("ARBITER_LISTEN_FD", listener.as_raw_fd().to_string())
        .env("ARBITER_BIND", format!("127.0.0.1:{port}"))
        .env("ARBITER_DB", &db_file)
        .env("ARBITER_SIM_START", "1000")
        .env("ARBITER_KEY_AUTONOMOUS", KEY_AU)
        .env("ARBITER_KEY_REMOTE", KEY_RC)
        .env("ARBITER_KEY_ESTOP", KEY_ES)
        .env("ARBITER_ADMIN_TOKEN", ADMIN_TOKEN)
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    let child = cmd.spawn().expect("spawn server binary");
    let env = Env {
        child,
        base: base.clone(),
        keys: Keys {
            autonomous: KEY_AU.as_bytes().to_vec(),
            remote: KEY_RC.as_bytes().to_vec(),
            estop: KEY_ES.as_bytes().to_vec(),
        },
        client: reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap(),
        _listener: listener,
    };
    wait_healthy(&base);
    env
}

pub fn tempdir() -> String {
    let p = std::env::temp_dir().join(format!(
        "motion-arbiter-restart-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p.to_string_lossy().to_string()
}

pub fn db_path(dir: &str) -> String {
    format!("{dir}/arbiter.sqlite")
}

// ---- 密码学：与服务端完全一致的真实 HMAC-SHA256 运算 ----

pub fn sign(key: &[u8], method: &str, path: &str, sim_at: &str, body: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(key).unwrap();
    mac.update(method.as_bytes());
    mac.update(b"\n");
    mac.update(path.as_bytes());
    mac.update(b"\n");
    mac.update(sim_at.as_bytes());
    mac.update(b"\n");
    mac.update(body.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

// ---- 请求构造 ----

pub fn cmd_body(seq: i64, issued_at: i64, lease_ms: i64, ttl_ms: i64, vx: f64) -> String {
    format!(
        r#"{{"seq":{seq},"lease_id":"l{seq}","vx":{vx},"wz":0.0,"issued_at":{issued_at},"lease_ms":{lease_ms},"ttl_ms":{ttl_ms}}}"#
    )
}

impl Env {
    pub fn get_json(&self, path: &str) -> serde_json::Value {
        let resp = self.client.get(format!("{}{}", self.base, path)).send().unwrap();
        assert!(resp.status().is_success(), "GET {path} -> {}", resp.status());
        resp.json().unwrap()
    }

    fn signed_post(
        &self,
        path: &str,
        key: &[u8],
        sim_at: Option<i64>,
        body: &str,
    ) -> reqwest::blocking::Response {
        let sim_hdr = sim_at.map(|t| t.to_string()).unwrap_or_default();
        let sig = sign(key, "POST", path, &sim_hdr, body);
        let mut req = self
            .client
            .post(format!("{}{}", self.base, path))
            .header("X-Signature", format!("hex={sig}"))
            .body(body.to_string());
        if let Some(t) = sim_at {
            req = req.header("X-Sim-At", t.to_string());
        }
        req.send().unwrap()
    }

    pub fn post_raw(&self, path: &str, sig: Option<&str>, body: &str) -> u16 {
        let mut req = self.client.post(format!("{}{}", self.base, path));
        if let Some(s) = sig {
            req = req.header("X-Signature", s);
        }
        req = req.header("X-Sim-At", "1000");
        req.body(body.to_string()).send().unwrap().status().as_u16()
    }

    fn post_command(
        &self,
        source: &str,
        key: &[u8],
        seq: i64,
        issued_at: i64,
        lease_ms: i64,
        ttl_ms: i64,
        vx: f64,
        at: i64,
    ) -> serde_json::Value {
        let path = format!("/v1/sources/{source}/commands");
        let body = cmd_body(seq, issued_at, lease_ms, ttl_ms, vx);
        let resp = self.signed_post(&path, key, Some(at), &body);
        let status = resp.status();
        let v: serde_json::Value = resp.json().unwrap();
        assert!(status.is_success(), "post_command failed: {v}");
        v
    }

    fn post_command_err(
        &self,
        source: &str,
        key: &[u8],
        seq: i64,
        issued_at: i64,
        lease_ms: i64,
        ttl_ms: i64,
        vx: f64,
        at: i64,
    ) -> (u16, serde_json::Value) {
        let path = format!("/v1/sources/{source}/commands");
        let body = cmd_body(seq, issued_at, lease_ms, ttl_ms, vx);
        let resp = self.signed_post(&path, key, Some(at), &body);
        (resp.status().as_u16(), resp.json().unwrap())
    }

    pub fn post_remote(
        &self,
        seq: i64,
        issued_at: i64,
        lease_ms: i64,
        ttl_ms: i64,
        vx: f64,
        at: i64,
    ) -> serde_json::Value {
        self.post_command("remote", &self.keys.remote, seq, issued_at, lease_ms, ttl_ms, vx, at)
    }

    pub fn post_remote_expect_err(
        &self,
        seq: i64,
        issued_at: i64,
        lease_ms: i64,
        ttl_ms: i64,
        vx: f64,
        at: i64,
    ) -> (u16, serde_json::Value) {
        self.post_command_err("remote", &self.keys.remote, seq, issued_at, lease_ms, ttl_ms, vx, at)
    }

    pub fn post_autonomous(
        &self,
        seq: i64,
        issued_at: i64,
        lease_ms: i64,
        ttl_ms: i64,
        vx: f64,
        at: i64,
    ) -> serde_json::Value {
        self.post_command("autonomous", &self.keys.autonomous, seq, issued_at, lease_ms, ttl_ms, vx, at)
    }

    pub fn post_estop(
        &self,
        seq: i64,
        event: &str,
        at: i64,
        reason: Option<&str>,
    ) -> serde_json::Value {
        let body = format!(
            r#"{{"seq":{seq},"event":"{event}","issued_at":{at},"reason":{}}}"#,
            reason.map(|r| format!("\"{r}\"")).unwrap_or_else(|| "null".into())
        );
        let resp = self.signed_post("/v1/estop", &self.keys.estop, Some(at), &body);
        assert!(resp.status().is_success());
        resp.json().unwrap()
    }

    pub fn post_estop_expect_err(&self, seq: i64, event: &str, at: i64) -> (u16, serde_json::Value) {
        let body = format!(r#"{{"seq":{seq},"event":"{event}","issued_at":{at}}}"#);
        let resp = self.signed_post("/v1/estop", &self.keys.estop, Some(at), &body);
        (resp.status().as_u16(), resp.json().unwrap())
    }

    pub fn post_release(&self, source: &str, seq: i64, lease_id: &str, at: i64) -> serde_json::Value {
        let path = format!("/v1/sources/{source}/leases/release");
        let body = format!(r#"{{"seq":{seq},"lease_id":"{lease_id}","issued_at":{at}}}"#);
        let key = if source == "remote" { &self.keys.remote } else { &self.keys.autonomous };
        let resp = self.signed_post(&path, key, Some(at), &body);
        assert!(resp.status().is_success());
        resp.json().unwrap()
    }

    pub fn post_release_expect_err(
        &self,
        source: &str,
        seq: i64,
        lease_id: &str,
        at: i64,
    ) -> (u16, serde_json::Value) {
        let path = format!("/v1/sources/{source}/leases/release");
        let body = format!(r#"{{"seq":{seq},"lease_id":"{lease_id}","issued_at":{at}}}"#);
        let key = if source == "remote" { &self.keys.remote } else { &self.keys.autonomous };
        let resp = self.signed_post(&path, key, Some(at), &body);
        (resp.status().as_u16(), resp.json().unwrap())
    }

    pub fn post_tick(&self, at: i64) -> serde_json::Value {
        let resp = self.signed_post("/v1/decisions/evaluate", &self.keys.estop, Some(at), "{}");
        assert!(resp.status().is_success());
        resp.json().unwrap()
    }
}
