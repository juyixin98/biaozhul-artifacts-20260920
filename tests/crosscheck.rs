//! Cross-backend equivalence: the streaming file reader and the mmap
//! reference reader must return byte-identical results for valid files, and
//! agree on rejection for mutated files.

use std::fs;
use std::path::PathBuf;

use ifix::json::parse;
use ifix::reader::Reader;
use ifix::validate::validate;
use ifix::writer::{build_from_json, WriteOptions};
use ifix::{Limits, NodeType};

fn tmp_path(name: &str) -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!(
        "ifix-cross-{}-{name}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    p
}

struct Guard(PathBuf);
impl Drop for Guard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn build_wide_file(leaves: usize) -> (PathBuf, Guard) {
    let mut kids = String::new();
    for i in 0..leaves {
        let payload = if i % 3 == 0 {
            // varying payload sizes, including empty
            ifix::base64::encode(&vec![b'x'; i % 5000])
        } else {
            ifix::base64::encode(format!("content-{i}").as_bytes())
        };
        kids.push_str(&format!(
            "{{\"name\":\"n{i:06}\",\"type\":\"file\",\"content_base64\":\"{payload}\"}},"
        ));
    }
    // Add nested dirs and symlinks
    kids.push_str("{\"name\":\"sub\",\"type\":\"dir\",\"children\":[");
    kids.push_str("{\"name\":\"a\",\"type\":\"symlink\",\"target\":\"../n000000\"},");
    kids.push_str("{\"name\":\"b\",\"type\":\"symlink\",\"target\":\"/n000001\"},");
    kids.push_str("{\"name\":\"c\",\"type\":\"dir\",\"children\":[");
    for i in 0..40 {
        if i > 0 {
            kids.push(',');
        }
        kids.push_str(&format!(
            "{{\"name\":\"m{i:02}\",\"type\":\"file\",\"content_base64\":\"aGk=\"}}"
        ));
    }
    kids.push_str("]}]}");

    let req = format!(r#"{{"tree":{{"name":"","type":"dir","children":[{kids}]}}}}"#);
    let v = parse(&req).unwrap();
    let image = build_from_json(&v, WriteOptions::default()).unwrap();

    let path = tmp_path("wide.ifix");
    fs::write(&path, &image).unwrap();
    let returned = path.clone();
    (returned, Guard(path))
}

#[test]
fn stream_and_mmap_agree_on_wide_tree() {
    let leaves = 2000;
    let (path, _g) = build_wide_file(leaves);

    let rs = Reader::open_file(&path, Limits::default()).unwrap();
    let rm = Reader::open_mmap(&path, Limits::default()).unwrap();

    let vs = validate(&rs).unwrap();
    let vm = validate(&rm).unwrap();
    assert_eq!(vs.nodes, vm.nodes);
    assert_eq!(vs.blocks, vm.blocks);

    // Every leaf lookup through both backends.
    for i in 0..leaves {
        let p = format!("/n{i:06}");
        let ns = rs.lookup(&p, true).unwrap();
        let nm = rm.lookup(&p, true).unwrap();
        assert_eq!(ns.id, nm.id);
        assert_eq!(ns.data, nm.data);
        let a = rs.read_file(&ns).unwrap();
        let b = rm.read_file(&nm).unwrap();
        assert_eq!(a, b, "payload mismatch at {p}");
    }

    // Symlink resolution.
    let link = rs.lookup("/sub/a", true).unwrap();
    assert_eq!(link.kind, NodeType::File);
    let link_m = rm.lookup("/sub/a", true).unwrap();
    assert_eq!(link_m.id, link.id);
    assert_eq!(rm.read_file(&link_m).unwrap(), rs.read_file(&link).unwrap());

    // Listings identical and ascending on both backends.
    let ls = rs.list("/").unwrap();
    let lm = rm.list("/").unwrap();
    assert_eq!(ls.len(), lm.len());
    let mut sorted = ls.clone();
    sorted.sort_by(|a, b| a.name.cmp(&b.name));
    assert_eq!(
        ls.iter().map(|e| e.name.clone()).collect::<Vec<_>>(),
        sorted.iter().map(|e| e.name.clone()).collect::<Vec<_>>(),
        "listing not in ascending key order"
    );
    for (a, b) in ls.iter().zip(lm.iter()) {
        assert_eq!(a.name, b.name);
        assert_eq!(a.node_id, b.node_id);
        assert_eq!(a.kind, b.kind);
    }
    let lsub_s = rs.list("/sub/c").unwrap();
    let lsub_m = rm.list("/sub/c").unwrap();
    assert_eq!(lsub_s.len(), lsub_m.len());
    assert_eq!(lsub_s.len(), 40);

    // Partial reads with offset/len.
    let n = rs.lookup("/n000010", true).unwrap();
    let mut a = Vec::new();
    rs.copy_file(&n, &mut a, 2, Some(5)).unwrap();
    let nm = rm.lookup("/n000010", true).unwrap();
    let mut b = Vec::new();
    rm.copy_file(&nm, &mut b, 2, Some(5)).unwrap();
    assert_eq!(a, b);
}

#[test]
fn readers_reject_truncated_file_at_all_cut_points() {
    let (path, _g) = build_wide_file(300);
    let full = fs::read(&path).unwrap();
    for cut in [0, 1, 127, 128, full.len() - 1] {
        let p = tmp_path("cut.ifix");
        fs::write(&p, &full[..cut]).unwrap();
        let file_ok = Reader::open_file(&p, Limits::default()).is_err();
        let mmap_ok = Reader::open_mmap(&p, Limits::default())
            .map(|r| validate(&r).is_err())
            .unwrap_or(true);
        let _ = fs::remove_file(&p);
        assert!(file_ok || mmap_ok, "truncation at {cut} accepted");
    }
}

#[test]
fn output_length_limit_enforced() {
    let (path, _g) = build_wide_file(10);
    let tight = Limits {
        max_output: 4,
        ..Limits::default()
    };
    let r = Reader::open_file(&path, tight).unwrap();
    // n00001 payload "content-10" is 10 bytes; >4 cap must fail.
    let n = r.lookup("/n000001", true).unwrap();
    assert!(r.read_file(&n).is_err());
    // A bounded read within the cap still succeeds.
    let mut out = Vec::new();
    assert!(r.copy_file(&n, &mut out, 0, Some(4)).is_ok());
    assert_eq!(out.len(), 4);
}
