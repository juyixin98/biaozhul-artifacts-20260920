use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo::rerun-if-changed=Cargo.lock");
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let lock = fs::read_to_string(manifest.join("Cargo.lock")).unwrap_or_default();
    let wasmtime_version = parse_lock_version(&lock, "wasmtime");
    let axum_version = parse_lock_version(&lock, "axum");
    println!("cargo::rustc-env=CARGO_PKG_VERSION_WASMTIME={wasmtime_version}");
    println!("cargo::rustc-env=CARGO_PKG_VERSION_AXUM={axum_version}");
}

/// 极简 Cargo.lock 解析：找 `name = "pkg"` 之后第一个 `version = "..."`。
fn parse_lock_version(lock: &str, name: &str) -> String {
    let mut lines = lock.lines();
    while let Some(line) = lines.next() {
        if line.trim() == format!("name = \"{name}\"") {
            for vline in lines.by_ref() {
                let t = vline.trim();
                if let Some(rest) = t.strip_prefix("version = ") {
                    return rest.trim_matches('"').to_string();
                }
                if t.starts_with("name = ") {
                    break;
                }
            }
        }
    }
    "unknown".to_string()
}
