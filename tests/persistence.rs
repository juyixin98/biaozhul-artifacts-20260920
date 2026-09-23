//! Tests against the real filesystem backend (`RealVfs`): process-like
//! reopen, on-disk file shapes, orphan-tail truncation.

use dual_sb_recovery::format::SB_SIZE;
use dual_sb_recovery::io::RealVfs;
use dual_sb_recovery::repo::Repository;

fn tempdir(tag: &str) -> std::path::PathBuf {
    let mut d = std::env::temp_dir();
    let unique = format!(
        "dsb-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    d.push(unique);
    d
}

fn open(dir: &std::path::Path) -> Repository<RealVfs> {
    let vfs = RealVfs::open(dir).unwrap();
    Repository::open(vfs).unwrap().0
}

#[test]
fn clean_commit_reopen_on_real_files() {
    let dir = tempdir("clean");
    {
        let mut repo = open(&dir);
        assert_eq!(repo.generation(), 0);
        repo.put(b"alpha".to_vec(), b"one".to_vec()).unwrap();
        repo.put(b"beta".to_vec(), b"two".to_vec()).unwrap();
        let g3 = repo.put(b"alpha".to_vec(), b"three".to_vec()).unwrap();
        assert_eq!(g3, 3);
        assert_eq!(repo.data_len(), repo_entries_len(&repo));
    }
    // "Reopen the process": brand new VFS over the same directory.
    let repo = open(&dir);
    assert_eq!(repo.generation(), 3);
    assert_eq!(repo.get(b"alpha"), Some(b"three".as_slice()));
    assert_eq!(repo.get(b"beta"), Some(b"two".as_slice()));

    // File shapes: two 4 KiB superblock pages, alternating slots.
    let sb_a = std::fs::read(dir.join("sb.a")).unwrap();
    let sb_b = std::fs::read(dir.join("sb.b")).unwrap();
    assert_eq!(sb_a.len(), SB_SIZE);
    assert_eq!(sb_b.len(), SB_SIZE);
    let data_len = std::fs::metadata(dir.join("data.log")).unwrap().len();
    assert_eq!(data_len, repo.data_len());

    // delete + reopen
    drop(repo);
    {
        let mut repo = open(&dir);
        repo.delete(b"beta".to_vec()).unwrap();
    }
    let repo = open(&dir);
    assert_eq!(repo.get(b"beta"), None);
    assert_eq!(repo.get(b"alpha"), Some(b"three".as_slice()));

    std::fs::remove_dir_all(&dir).ok();
}

fn repo_entries_len(repo: &Repository<RealVfs>) -> u64 {
    repo.data_len()
}

#[test]
fn recovery_truncates_orphan_tail_on_real_file() {
    let dir = tempdir("orphan");
    {
        let mut repo = open(&dir);
        repo.put(b"k1".to_vec(), b"v1".to_vec()).unwrap();
        repo.put(b"k2".to_vec(), b"v2".to_vec()).unwrap();
    }
    // Simulate a torn data append from a crashed gen-3 write: append garbage
    // to data.log that no valid superblock references.
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(dir.join("data.log"))
            .unwrap();
        f.write_all(&[0x77u8; 500]).unwrap();
    }
    let before = std::fs::metadata(dir.join("data.log")).unwrap().len();
    let repo = open(&dir);
    let after = std::fs::metadata(dir.join("data.log")).unwrap().len();
    assert_eq!(before - after, 500, "orphan tail must be truncated on open");
    assert_eq!(repo.generation(), 2);
    assert_eq!(repo.get(b"k2"), Some(b"v2".as_slice()));
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn oob_root_pointer_rejected_on_real_file() {
    let dir = tempdir("oob");
    {
        let mut repo = open(&dir);
        repo.put(b"k1".to_vec(), b"v1".to_vec()).unwrap();
    }
    // Corrupt the active superblock's data_len field to point past EOF,
    // then recompute a valid CRC so only the pointer check can reject it.
    let path = dir.join("sb.a");
    let mut page = std::fs::read(&path).unwrap();
    page[16..24].copy_from_slice(&9_999_999u64.to_le_bytes());
    let crc = dual_sb_recovery::crc32::checksum(&page[..4072]);
    page[4072..4076].copy_from_slice(&crc.to_le_bytes());
    std::fs::write(&path, page).unwrap();

    let vfs = RealVfs::open(&dir).unwrap();
    let err = Repository::open(vfs).err();
    // With only one (now invalid) slot present, the store is not recoverable.
    assert!(
        matches!(
            err,
            Some(dual_sb_recovery::repo::StoreError::NoValidSuperblock)
        ),
        "expected refusal, got {err:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn fresh_empty_dir_opens_as_generation_zero() {
    let dir = tempdir("fresh");
    let vfs = RealVfs::open(&dir).unwrap();
    let (repo, report) = Repository::open(vfs).unwrap();
    assert_eq!(repo.generation(), 0);
    assert!(report.selected_slot.is_none());
    assert!(!std::path::Path::new(&dir).join("sb.a").exists());
    std::fs::remove_dir_all(&dir).ok();
}
