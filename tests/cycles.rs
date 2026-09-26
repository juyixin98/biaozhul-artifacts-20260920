//! Symlink cycle and depth tests, plus blob-interval overlap mutation.
//!
//! A file may legitimately contain symlinks whose targets form a cycle —
//! the file itself is well-formed (symlink targets are opaque payload). The
//! resolver must detect the cycle and bounded chains must resolve.

use ifix::json::parse;
use ifix::reader::Reader;
use ifix::validate::validate;
use ifix::writer::{build_from_json, WriteOptions};
use ifix::{Error, Limits, NodeType};

fn build(req: &str) -> Vec<u8> {
    let v = parse(req).unwrap();
    build_from_json(&v, WriteOptions::default()).unwrap()
}

const CYCLIC: &str = r#"{"tree":{"name":"","type":"dir","children":[
  {"name":"a","type":"symlink","target":"b"},
  {"name":"b","type":"symlink","target":"a"},
  {"name":"self","type":"symlink","target":"self"},
  {"name":"dir","type":"dir","children":[
    {"name":"up","type":"symlink","target":"../dir/up"}
  ]},
  {"name":"real","type":"file","content_base64":"aGk="},
  {"name":"good","type":"symlink","target":"/real"}
]}}"#;

#[test]
fn file_with_cycles_is_itself_valid() {
    let img = build(CYCLIC);
    let r = Reader::new(
        Box::new(ifix::reader::SliceSource::new(img)),
        Limits::default(),
    )
    .unwrap();
    validate(&r).unwrap();
}

#[test]
fn direct_cycle_is_detected() {
    let img = build(CYCLIC);
    let r = Reader::new(
        Box::new(ifix::reader::SliceSource::new(img)),
        Limits::default(),
    )
    .unwrap();
    assert!(matches!(
        r.lookup("/a", true),
        Err(Error::LinkCycleOrDepth(_))
    ));
    assert!(matches!(
        r.lookup("/b", true),
        Err(Error::LinkCycleOrDepth(_))
    ));
    assert!(matches!(
        r.lookup("/self", true),
        Err(Error::LinkCycleOrDepth(_))
    ));
    assert!(matches!(
        r.lookup("/dir/up", true),
        Err(Error::LinkCycleOrDepth(_))
    ));
}

#[test]
fn lstat_does_not_follow_and_breaks_no_ground() {
    let img = build(CYCLIC);
    let r = Reader::new(
        Box::new(ifix::reader::SliceSource::new(img)),
        Limits::default(),
    )
    .unwrap();
    let a = r.lookup("/a", false).unwrap();
    assert_eq!(a.kind, NodeType::Symlink);
    // Non-cyclic links resolve normally.
    assert_eq!(r.lookup("/good", true).unwrap().kind, NodeType::File);
}

#[test]
fn finite_chain_resolves() {
    // chain: l1->l2->...->lN->real
    let mut kids = String::new();
    const N: usize = 8;
    for i in 1..N {
        kids.push_str(&format!(
            "{{\"name\":\"l{i}\",\"type\":\"symlink\",\"target\":\"l{}\"}},",
            i + 1
        ));
    }
    kids.push_str(&format!(
        "{{\"name\":\"l{N}\",\"type\":\"symlink\",\"target\":\"real\"}},"
    ));
    kids.push_str("{\"name\":\"real\",\"type\":\"file\",\"content_base64\":\"eWVz\"}");
    let req = format!(r#"{{"tree":{{"name":"","type":"dir","children":[{kids}]}}}}"#);
    let img = build(&req);
    let r = Reader::new(
        Box::new(ifix::reader::SliceSource::new(img)),
        Limits::default(),
    )
    .unwrap();
    let n = r.lookup("/l1", true).unwrap();
    assert_eq!(n.kind, NodeType::File);
    assert_eq!(r.read_file(&n).unwrap(), b"yes");
}

#[test]
fn chain_over_depth_limit_is_rejected() {
    let tight = Limits {
        max_link_depth: 3,
        ..Limits::default()
    };
    let img = build(CYCLIC);
    let r = Reader::new(Box::new(ifix::reader::SliceSource::new(img)), tight).unwrap();
    assert!(matches!(
        r.lookup("/a", true),
        Err(Error::LinkCycleOrDepth(_))
    ));
}

#[test]
fn blob_interval_overlap_is_rejected() {
    use ifix::format::{HEADER_SIZE, NODE_SIZE};
    let img = build(CYCLIC);
    // real is node id 5 (a,b,self,dir, then... actually locate by lookup):
    // mutate the real file's blob interval to collide with symlink "a"'s blob.
    let r = Reader::new(
        Box::new(ifix::reader::SliceSource::new(img.clone())),
        Limits::default(),
    )
    .unwrap();
    let real = r.lookup("/real", false).unwrap();
    let a = r.lookup("/a", false).unwrap();
    let mut bad = img;
    let base = HEADER_SIZE as usize + real.id as usize * NODE_SIZE as usize;
    // Point real's blob at a's blob offset with a length that overlaps.
    bad[base + 16..base + 24].copy_from_slice(&(a.data.0).to_le_bytes());
    bad[base + 24..base + 32].copy_from_slice(&(a.data.1 + 1).to_le_bytes());
    let r2 = Reader::new(
        Box::new(ifix::reader::SliceSource::new(bad)),
        Limits::default(),
    )
    .unwrap();
    assert!(validate(&r2).is_err());
}
