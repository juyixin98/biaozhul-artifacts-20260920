//! 验收测试：随机键对照 BTreeMap 模型。
//! 覆盖：空键/空值、长公共前缀、跨块扫描、不同块大小与重启间隔。

use sst::{MemStorage, TableOptions, TableReader, TableWriter};
use std::collections::BTreeMap;

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
    fn bytes(&mut self, len: usize) -> Vec<u8> {
        (0..len).map(|_| (self.next() & 0xFF) as u8).collect()
    }
}

fn build(model: &BTreeMap<Vec<u8>, Vec<u8>>, opts: TableOptions) -> MemStorage {
    let mem = MemStorage::new();
    let mut w = TableWriter::new(mem.clone(), opts);
    for (k, v) in model {
        w.add(k, v).expect("add");
    }
    let stats = w.finish().expect("finish");
    assert_eq!(stats.num_entries as usize, model.len());
    mem
}

/// 全面对照：点查（命中+未命中）、全表扫描、随机范围扫描、严格校验。
fn check_against_model(model: &BTreeMap<Vec<u8>, Vec<u8>>, opts: TableOptions, seed: u64) {
    let mem = build(model, opts);
    let t = TableReader::open(mem.clone()).expect("open");

    // 严格校验必须通过
    let report = t.verify().expect("verify must pass on a freshly built table");
    assert_eq!(report.num_entries as usize, model.len());
    if let Some(first) = model.keys().next() {
        assert_eq!(report.first_key.as_ref(), Some(first));
        assert_eq!(report.last_key.as_ref(), model.keys().last());
    }

    // 点查：全部命中
    for (k, v) in model {
        assert_eq!(
            t.get(k).expect("get").as_deref(),
            Some(v.as_slice()),
            "get miss for key {k:?}"
        );
    }

    // 点查：随机未命中键
    let mut rng = Rng(seed);
    for _ in 0..500 {
        let len = rng.below(24);
        let k = rng.bytes(len);
        if !model.contains_key(&k) {
            assert_eq!(t.get(&k).expect("get miss"), None, "false hit for {k:?}");
        }
    }

    // 全表扫描 == 模型迭代
    let all: Vec<_> = t
        .scan(b"", None)
        .collect::<Result<Vec<_>, _>>()
        .expect("full scan");
    let want: Vec<(Vec<u8>, Vec<u8>)> =
        model.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    assert_eq!(all, want, "full scan mismatch");

    // 随机范围扫描 [start, end)
    let keys: Vec<&Vec<u8>> = model.keys().collect();
    for _ in 0..200 {
        let (start, end) = if keys.is_empty() || rng.below(4) == 0 {
            let (la, lb) = (rng.below(16), rng.below(16));
            (rng.bytes(la), rng.bytes(lb))
        } else {
            (keys[rng.below(keys.len())].clone(), keys[rng.below(keys.len())].clone())
        };
        let (start, end) = if start <= end { (start, end) } else { (end, start) };
        let got: Vec<_> = t
            .scan(&start, Some(&end))
            .collect::<Result<Vec<_>, _>>()
            .expect("range scan");
        let want: Vec<(Vec<u8>, Vec<u8>)> = model
            .range(start.clone()..end.clone())
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        assert_eq!(got, want, "range scan [{start:?}, {end:?}) mismatch");
    }
}

#[test]
fn random_binary_keys_tiny_blocks() {
    // 小数据块（128B）强制产生大量块，覆盖跨块扫描
    let mut rng = Rng(42);
    let mut model = BTreeMap::new();
    while model.len() < 1500 {
        let (lk, lv) = (rng.below(24), rng.below(48));
        let k = rng.bytes(lk); // 长度 0..23，含空键
        let v = rng.bytes(lv);
        model.insert(k, v);
    }
    check_against_model(
        &model,
        TableOptions {
            block_size: 128,
            restart_interval: 4,
        },
        7,
    );
}

#[test]
fn long_common_prefix_keys() {
    // 300 字节公共前缀 + 计数器后缀：前缀压缩收益最大、跨块重启点压力最大
    let prefix = vec![b'p'; 300];
    let mut model = BTreeMap::new();
    for i in 0u32..500 {
        let mut k = prefix.clone();
        k.extend_from_slice(&i.to_be_bytes());
        model.insert(k, format!("value-{i}").into_bytes());
    }
    check_against_model(
        &model,
        TableOptions {
            block_size: 256,
            restart_interval: 8,
        },
        11,
    );
}

#[test]
fn empty_keys_and_values() {
    let mut model = BTreeMap::new();
    model.insert(vec![], vec![]); // 空键空值
    model.insert(vec![], b"empty-key".to_vec()); // 覆盖同键 → 保留后者
    model.insert(b"a".to_vec(), vec![]); // 空值
    model.insert(b"ab".to_vec(), b"x".to_vec());
    model.insert(vec![0x00], vec![0x00; 3]); // 含 NUL 的键值
    model.insert(vec![0xFF; 5], vec![]);
    check_against_model(
        &model,
        TableOptions {
            block_size: 64,
            restart_interval: 2,
        },
        13,
    );
}

#[test]
fn restart_interval_one() {
    let mut rng = Rng(99);
    let mut model = BTreeMap::new();
    while model.len() < 300 {
        let (lk, lv) = (1 + rng.below(8), rng.below(16));
        model.insert(rng.bytes(lk), rng.bytes(lv));
    }
    check_against_model(
        &model,
        TableOptions {
            block_size: 96,
            restart_interval: 1, // 每条目都是重启点
        },
        17,
    );
}

#[test]
fn large_values_span_blocks() {
    let mut rng = Rng(1234);
    let mut model = BTreeMap::new();
    for i in 0..50 {
        let k = format!("big/{:04}", i).into_bytes();
        let lv = 1024 + rng.below(3072);
        let v = rng.bytes(lv); // 1~4KB 值，单条目即可超块
        model.insert(k, v);
    }
    check_against_model(
        &model,
        TableOptions {
            block_size: 1024,
            restart_interval: 4,
        },
        23,
    );
}

#[test]
fn single_entry_and_empty_table() {
    // 单条目
    let mut model = BTreeMap::new();
    model.insert(b"only".to_vec(), b"one".to_vec());
    check_against_model(&model, TableOptions::default(), 29);

    // 空表
    let model: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
    let mem = build(&model, TableOptions::default());
    let t = TableReader::open(mem).expect("open empty table");
    assert_eq!(t.get(b"anything").unwrap(), None);
    assert_eq!(t.scan(b"", None).count(), 0);
    let r = t.verify().expect("verify empty table");
    assert_eq!(r.num_entries, 0);
    assert_eq!(r.num_blocks, 0);
}

#[test]
fn writer_rejects_unordered_and_duplicate_keys() {
    let mem = MemStorage::new();
    let mut w = TableWriter::new(mem, TableOptions::default());
    w.add(b"b", b"1").unwrap();
    assert!(w.add(b"b", b"2").is_err(), "duplicate key must fail");
    assert!(w.add(b"a", b"3").is_err(), "decreasing key must fail");
    w.add(b"c", b"4").unwrap();
    w.finish().unwrap();
}
