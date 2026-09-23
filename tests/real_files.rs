//! 真实文件后端集成测试：跑在真实 fsync 上，并通过**进程外手段**
//! （直接截断/翻转文件字节）模拟介质损坏，验证引擎能在真实目录上恢复。

use std::path::{Path, PathBuf};

use dual_superblock::format::{self, PAGE_SIZE};
use dual_superblock::io_layer::RealStorage;
use dual_superblock::store::Repository;

fn temp_dir(tag: &str) -> PathBuf {
    let mut d = std::env::temp_dir();
    let unique = format!(
        "dual-sb-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    d.push(unique);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn open_repo(dir: &Path) -> Repository<RealStorage> {
    let st = RealStorage::open(dir).expect("打开真实文件失败");
    Repository::open(st).expect("恢复真实仓库失败")
}

fn format_repo(dir: &Path) -> Repository<RealStorage> {
    RealStorage::create_files(dir).unwrap();
    let st = RealStorage::open(dir).unwrap();
    Repository::format(st).unwrap()
}

#[test]
fn real_file_clean_roundtrip_and_reopen() {
    let dir = temp_dir("clean");
    {
        let mut repo = format_repo(&dir);
        repo.put_batch([
            ("alpha".as_bytes(), "one".as_bytes()),
            ("beta".as_bytes(), "two".as_bytes()),
        ])
        .unwrap();
        repo.put_batch([("gamma".as_bytes(), "三".as_bytes())])
            .unwrap();
        repo.delete_batch([b"alpha".as_slice()]).unwrap();
    }
    // 重新打开（新的 RealStorage 句柄，等同于进程重启）。
    let repo = open_repo(&dir);
    assert_eq!(repo.generation(), 3);
    assert!(repo.get(b"alpha").is_none());
    assert_eq!(repo.get(b"beta"), Some("two".as_bytes()));
    assert_eq!(repo.get(b"gamma"), Some("三".as_bytes()));

    // 磁盘文件尺寸不变量：超级块固定 8192。
    let sf_len = std::fs::metadata(dir.join(format::SUPER_FILE))
        .unwrap()
        .len();
    assert_eq!(sf_len, (2 * PAGE_SIZE) as u64);
    let data_len = std::fs::metadata(dir.join(format::DATA_FILE))
        .unwrap()
        .len();
    assert!(data_len > 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn real_file_torn_super_slot_falls_back() {
    let dir = temp_dir("torn-sb");
    // 写 2 代次：gen1 在槽1，gen2 在槽0。
    {
        let mut repo = format_repo(&dir);
        repo.put_batch([(b"k".as_slice(), b"v1".as_slice())])
            .unwrap();
        repo.put_batch([(b"k".as_slice(), b"v2".as_slice())])
            .unwrap();
    }
    // 介质损坏：把槽0（gen2）页尾尾戳区域翻转，整页 CRC 立即失效；
    // 槽1（gen1）保持完好。注意槽0尾戳偏移是 PAGE_SIZE-6，不是文件尾-6。
    let sf_path = dir.join(format::SUPER_FILE);
    let mut sf = std::fs::read(&sf_path).unwrap();
    sf[PAGE_SIZE - 6] ^= 0xFF; // 落在槽0的尾戳内部（< PAGE_SIZE）
    std::fs::write(&sf_path, &sf).unwrap();

    let repo = open_repo(&dir);
    assert_eq!(repo.generation(), 1, "应回退到槽1中的代次1");
    assert_eq!(repo.get(b"k"), Some(b"v1".as_slice()));

    // 回退后仍可继续写入：代次从 1 推进。
    let mut repo = repo;
    let snap = repo
        .put_batch([(b"k".as_slice(), b"v3".as_slice())])
        .unwrap();
    assert_eq!(snap.generation, 2);
    drop(repo);
    assert_eq!(open_repo(&dir).generation(), 2);

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn real_file_both_slots_corrupt_is_an_error_not_silence() {
    let dir = temp_dir("both-bad");
    {
        let mut repo = format_repo(&dir);
        repo.put_batch([(b"k".as_slice(), b"v1".as_slice())])
            .unwrap();
    }
    let sf_path = dir.join(format::SUPER_FILE);
    let mut sf = std::fs::read(&sf_path).unwrap();
    sf[0] ^= 0xFF; // 槽0 魔数
    sf[PAGE_SIZE] ^= 0xFF; // 槽1 魔数
    std::fs::write(&sf_path, &sf).unwrap();

    let st = RealStorage::open(&dir).unwrap();
    let err = match Repository::open(st) {
        Err(e) => e,
        Ok(_) => panic!("两槽皆坏必须报错"),
    };
    let msg = err.to_string();
    assert!(msg.contains("两个超级块槽位均无效"), "实际: {msg}");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn real_file_truncated_data_tail_is_tolerated() {
    let dir = temp_dir("trunc-data");
    {
        let mut repo = format_repo(&dir);
        repo.put_batch([(b"a".as_slice(), b"1".as_slice())])
            .unwrap();
        repo.put_batch([(b"b".as_slice(), b"2".as_slice())])
            .unwrap();
    }
    // 介质在尾部丢了 7 字节（最后一次提交的记录被截断），同时把槽0(gen2)
    // 的超级块也毁掉——此时引擎必须回退到 gen1，而不是崩溃或读越界。
    let data_path = dir.join(format::DATA_FILE);
    let meta = std::fs::metadata(&data_path).unwrap();
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .open(&data_path)
        .unwrap();
    use std::io::Write;
    f.set_len(meta.len() - 7).unwrap();
    f.flush().unwrap();
    drop(f);

    let sf_path = dir.join(format::SUPER_FILE);
    let mut sf = std::fs::read(&sf_path).unwrap();
    sf[PAGE_SIZE - 2] ^= 0xFF; // 毁掉槽0尾戳 → 整页 CRC 失效
    std::fs::write(&sf_path, sf).unwrap();

    let repo = open_repo(&dir);
    assert_eq!(repo.generation(), 1);
    assert_eq!(repo.get(b"a"), Some(b"1".as_slice()));
    assert!(repo.get(b"b").is_none());

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn real_file_rejects_pointer_beyond_eof() {
    let dir = temp_dir("oob");
    {
        let mut repo = format_repo(&dir);
        repo.put_batch([(b"safe".as_slice(), b"ok".as_slice())])
            .unwrap(); // gen1 → 槽1
    }
    // 在槽0伪造一个高代次超级块，根指针指向远超 EOF 的位置（页 CRC/尾戳都合法）。
    let bogus = format::SuperBlock {
        generation: 42,
        root: format::Ptr {
            offset: 99_999_999,
            len: 64,
            crc: 0xCAFE,
        },
    };
    let sf_path = dir.join(format::SUPER_FILE);
    let mut sf = std::fs::read(&sf_path).unwrap();
    let page = format::encode_super_page(&bogus);
    sf[0..PAGE_SIZE].copy_from_slice(&page);
    std::fs::write(&sf_path, sf).unwrap();

    // 越界候选必须被拒绝并回退 gen1，而不是 panic / 读越界。
    let repo = open_repo(&dir);
    assert_eq!(repo.generation(), 1);
    assert_eq!(repo.get(b"safe"), Some(b"ok".as_slice()));

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn real_file_open_unformatted_dir_is_notformatted() {
    let dir = temp_dir("unformatted");
    // 目录存在但无文件：open 直接报 NotFound（来自 I/O 层）。
    let err = match RealStorage::open(&dir) {
        Err(e) => e,
        Ok(_) => panic!("未格式化目录应打开失败"),
    };
    assert_eq!(err.kind(), std::io::ErrorKind::NotFound);
    std::fs::remove_dir_all(&dir).ok();
}
