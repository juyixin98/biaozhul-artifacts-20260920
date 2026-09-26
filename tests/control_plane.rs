//! Integration tests for the JSON control plane.

use cdict::control::execute;
use cdict::json::{self, Json};
use std::fs;
use std::path::PathBuf;

fn tmp_path(name: &str) -> PathBuf {
    let mut dir = std::env::temp_dir();
    dir.push(format!("cdict-test-{}-{name}", std::process::id()));
    dir
}

struct TmpFile(PathBuf);
impl TmpFile {
    fn new(name: &str, content: &[u8]) -> Self {
        let p = tmp_path(name);
        fs::write(&p, content).unwrap();
        TmpFile(p)
    }
    fn path(&self) -> String {
        self.0.to_string_lossy().into_owned()
    }
}
impl Drop for TmpFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn run(req: &str) -> (Json, Vec<u8>) {
    let doc = Json::parse_str(req).unwrap();
    let mut out = Vec::new();
    let resp = execute(&doc, &mut out).expect("execute should succeed");
    (resp, out)
}

#[test]
fn encode_decode_via_control_plane() {
    let rows_file = TmpFile::new("rows.json", br#"["a", null, "b", "a", ""]"#);
    let bin_file = tmp_path("out.cdc");
    let back_file = tmp_path("back.json");

    let (resp, _) = run(&format!(
        r#"{{"op":"encode","input":"{}","output":"{}","segment_rows":2}}"#,
        rows_file.path(),
        bin_file.to_string_lossy()
    ));
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    assert_eq!(resp.get("rows"), Some(&Json::Int(5)));
    assert_eq!(resp.get("segments"), Some(&Json::Int(3)));

    let (resp2, _) = run(&format!(
        r#"{{"op":"decode","input":"{}","output":"{}"}}"#,
        bin_file.to_string_lossy(),
        back_file.to_string_lossy()
    ));
    assert_eq!(resp2.get("ok"), Some(&Json::Bool(true)));

    let back = fs::read_to_string(&back_file).unwrap();
    let parsed = Json::parse_str(&back).unwrap();
    assert_eq!(
        parsed,
        Json::Array(vec![
            Json::String("a".into()),
            Json::Null,
            Json::String("b".into()),
            Json::String("a".into()),
            Json::String("".into()),
        ])
    );

    fs::remove_file(&bin_file).ok();
    fs::remove_file(&back_file).ok();
}

#[test]
fn merge_via_control_plane() {
    let f1 = TmpFile::new("r1.json", br#"["x","y",null]"#);
    let f2 = TmpFile::new("r2.json", br#"["y","z"]"#);
    let s1 = tmp_path("s1.cdc");
    let s2 = tmp_path("s2.cdc");
    let merged = tmp_path("merged.cdc");
    let back = tmp_path("mback.json");

    for (f, s, seg) in [(f1.path(), s1.clone(), 1u64), (f2.path(), s2.clone(), 10)] {
        let (r, _) = run(&format!(
            r#"{{"op":"encode","input":"{f}","output":"{}","segment_rows":{seg}}}"#,
            s.to_string_lossy()
        ));
        assert_eq!(r.get("ok"), Some(&Json::Bool(true)));
    }

    let (resp, _) = run(&format!(
        r#"{{"op":"merge","inputs":["{}","{}"],"output":"{}","segment_rows":2}}"#,
        s1.to_string_lossy(),
        s2.to_string_lossy(),
        merged.to_string_lossy()
    ));
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    assert_eq!(resp.get("rows"), Some(&Json::Int(5)));
    assert_eq!(resp.get("global_dict_entries"), Some(&Json::Int(3)));

    let (r2, _) = run(&format!(
        r#"{{"op":"decode","input":"{}","output":"{}"}}"#,
        merged.to_string_lossy(),
        back.to_string_lossy()
    ));
    assert_eq!(r2.get("ok"), Some(&Json::Bool(true)));
    let parsed = Json::parse_str(&fs::read_to_string(&back).unwrap()).unwrap();
    assert_eq!(
        parsed,
        Json::Array(vec![
            Json::String("x".into()),
            Json::String("y".into()),
            Json::Null,
            Json::String("y".into()),
            Json::String("z".into()),
        ])
    );

    for p in [s1, s2, merged, back] {
        fs::remove_file(p).ok();
    }
}

#[test]
fn inspect_reports_stats() {
    let rows = TmpFile::new("insp.json", br#"["a","b",null,"a",null]"#);
    let bin = tmp_path("insp.cdc");
    let (r, _) = run(&format!(
        r#"{{"op":"encode","input":"{}","output":"{}"}}"#,
        rows.path(),
        bin.to_string_lossy()
    ));
    assert_eq!(r.get("ok"), Some(&Json::Bool(true)));

    let (resp, _) = run(&format!(
        r#"{{"op":"inspect","input":"{}"}}"#,
        bin.to_string_lossy()
    ));
    assert_eq!(resp.get("rows"), Some(&Json::Int(5)));
    assert_eq!(resp.get("null_rows"), Some(&Json::Int(2)));
    assert_eq!(resp.get("distinct_values"), Some(&Json::Int(2)));
    fs::remove_file(&bin).ok();
}

#[test]
fn error_response_on_bad_request() {
    let doc = Json::parse_str(r#"{"op":"frobnicate"}"#).unwrap();
    let mut out = Vec::new();
    let err = execute(&doc, &mut out).unwrap_err();
    let rendered = json::to_string(&Json::Object(vec![
        ("ok".into(), Json::Bool(false)),
        ("error".into(), Json::String(err.to_string())),
    ]));
    assert!(rendered.contains("\"ok\":false"));
    assert!(rendered.contains("frobnicate"));
}

#[test]
fn limit_override_is_applied() {
    let rows = TmpFile::new("lim.json", br#"["a","b","c"]"#);
    let bin = tmp_path("lim.cdc");
    let doc = Json::parse_str(&format!(
        r#"{{"op":"encode","input":"{}","output":"{}","limits":{{"max_dict_entries":2}}}}"#,
        rows.path(),
        bin.to_string_lossy()
    ))
    .unwrap();
    let mut out = Vec::new();
    let err = execute(&doc, &mut out).unwrap_err();
    assert!(matches!(err, cdict::Error::LimitExceeded(_)));
    fs::remove_file(&bin).ok();
}
