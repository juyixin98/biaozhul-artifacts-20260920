//! 构建脚本：把 `wat/*.wat` 文本样例编译为 `wasm/*.wasm`，
//! 并通过 `cargo:rerun-if-changed` 实现增量构建。
use std::fs;
use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-changed=wat");
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let wat_dir = manifest.join("wat");
    let out_dir = manifest.join("wasm");
    fs::create_dir_all(&out_dir).expect("create wasm dir");

    for entry in fs::read_dir(&wat_dir).expect("read wat dir") {
        let path = entry.expect("read dir entry").path();
        if path.extension().and_then(|e| e.to_str()) == Some("wat") {
            let bytes = fs::read(&path).expect("read wat");
            let wasm = wat::parse_bytes(&bytes)
                .unwrap_or_else(|e| panic!("compile {}: {e}", path.display()));
            let stem = path.file_stem().unwrap();
            fs::write(
                out_dir.join(format!("{}.wasm", stem.to_str().unwrap())),
                &wasm,
            )
            .expect("write wasm");
            println!("cargo:rerun-if-changed={}", path.display());
        }
    }
}
