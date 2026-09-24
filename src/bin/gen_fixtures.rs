//! 夹具生成工具：把确定性夹具（policy.json、构建器私钥、四份示例请求）
//! 写入指定目录。
//!
//! 用法：
//!     cargo run --bin gen-fixtures -- [目录，默认 fixtures]

use std::path::Path;

use provenance::fixture;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "fixtures".to_string());
    let path = Path::new(&dir);

    let bundle = fixture::FixtureBundle {
        dir: path.to_path_buf(),
        ..fixture::generate()
    };
    fixture::persist(&bundle, path)?;

    println!("夹具已写入 {dir}/");
    println!("  policy.json");
    println!("  builders/<builder_id>.secret.hex");
    println!("  examples/01_valid.json");
    println!("  examples/02_output_substituted.json");
    println!("  examples/03_material_missing.json");
    println!("  examples/04_cross_builder_reuse.json");
    Ok(())
}
