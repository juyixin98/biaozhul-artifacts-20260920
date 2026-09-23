//! Crash-injection acceptance tests over the simulated page-cache media.
//!
//! Every commit point is crashed before/after each write and sync, with
//! None/Half/Short torn-write prefixes. Recovery must always pick the
//! highest complete, valid generation and refuse out-of-bounds pointers.

use dual_sb_recovery::demo::{self, run_single};
use dual_sb_recovery::format::{Op, RecordHeader, Superblock, SB_SIZE};
use dual_sb_recovery::io::{
    CrashPoint, CrashPolicy, FileId, IoError, SimVfs, Torn, Vfs,
};
use dual_sb_recovery::repo::{Repository, StoreError};

fn no_crash() -> CrashPolicy {
    CrashPolicy::never()
}

fn commit_n(sim: SimVfs, n: u64) -> SimVfs {
    let mut s = sim;
    for g in 1..=n {
        let (mut repo, _) = Repository::open(s).unwrap();
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        s = repo.into_vfs();
    }
    s
}

fn recover_owned(sim: SimVfs) -> (Repository<SimVfs>, dual_sb_recovery::repo::RecoveryReport) {
    Repository::open(sim).unwrap()
}

// ---------------------------------------------------------------------------
// Full crash matrix
// ---------------------------------------------------------------------------

#[test]
fn matrix_crash_during_commit_3() {
    let cases = demo::run_matrix();
    assert_eq!(cases.len(), CrashPoint::ALL.len() * 3);
    for c in &cases {
        assert!(
            c.passed(),
            "point={} torn={}: opened={} gen={:?} want={} value={:?} orphan={} err={:?}",
            c.point,
            c.torn,
            c.opened,
            c.recovered_generation,
            c.expected_generation,
            c.last_value,
            c.orphan_bytes_truncated,
            c.error
        );
    }
}

#[test]
fn matrix_crash_during_commit_2_first_rollback() {
    let cases = demo::run_round2_matrix();
    for c in &cases {
        assert!(
            c.passed(),
            "point={}: gen={:?} want={} value={:?}",
            c.point, c.recovered_generation, c.expected_generation, c.last_value
        );
    }
}

#[test]
fn successful_crash_point_preserves_commit() {
    // RootSyncEnd is past the final barrier: the crash error is returned,
    // but gen 3 is already durable and survives the reboot.
    let c = run_single(
        CrashPolicy::new(CrashPoint::RootSyncEnd, Torn::None),
        3,
    );
    assert!(c.passed());
    assert_eq!(c.recovered_generation, Some(3));
    assert_eq!(c.last_value.as_deref(), Some("v3"));
    assert_eq!(c.orphan_bytes_truncated, 0);
}

#[test]
fn crash_before_data_sync_always_rolls_back() {
    for point in [
        CrashPoint::DataWriteBegin,
        CrashPoint::DataWriteEnd,
        CrashPoint::DataSyncBegin,
    ] {
        for torn in [Torn::None, Torn::Half, Torn::Short] {
            let c = run_single(CrashPolicy::new(point, torn), 3);
            assert_eq!(c.recovered_generation, Some(2), "{point:?} {torn:?}");
            assert_eq!(c.last_value.as_deref(), Some("v2"), "{point:?} {torn:?}");
        }
    }
}

// ---------------------------------------------------------------------------
// Half-page write — new superblock page torn, old slot selected
// ---------------------------------------------------------------------------

#[test]
fn half_page_new_superblock_falls_back_to_old_slot() {
    let mut sim = SimVfs::fresh(no_crash());
    for g in 1u64..=2 {
        let (mut repo, _) = Repository::open(sim).unwrap();
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        sim = repo.into_vfs();
    }
    let (mut repo, _) = Repository::open(sim).unwrap();
    repo.arm_crash_policy(CrashPolicy::new(CrashPoint::RootWriteEnd, Torn::Half));
    let res = repo.put(b"k".to_vec(), b"v3".to_vec());
    assert!(matches!(res, Err(StoreError::Io(IoError::SimulatedCrash))));
    let sim = repo.into_vfs();

    // sb.a already held gen 1's full page; the torn overwrite replaced only
    // the first 2048 bytes with gen 3's page prefix, leaving gen 1's old
    // tail. The stitched page is still 4 KiB but its CRC must fail.
    let torn_page = sim.durable_bytes(FileId::SbA).unwrap();
    assert_eq!(torn_page.len(), SB_SIZE);
    assert!(
        Superblock::decode(&torn_page, sim.durable_len(FileId::Data)).is_err()
    );
    // Its first half really is the gen-3 page (new generation field), while
    // the tail is gen-1 leftovers — proof of a torn in-place overwrite.
    assert_eq!(u64::from_le_bytes(torn_page[8..16].try_into().unwrap()), 3);

    // Recovery: gen 3 absent, gen 2 selected from the other slot.
    let (repo, report) = recover_owned(sim.reopen(no_crash()));
    assert_eq!(repo.generation(), 2);
    assert_eq!(repo.get(b"k"), Some(b"v2".as_slice()));
    assert_eq!(report.selected_slot, Some(1));
    assert!(report
        .slots
        .iter()
        .find(|s| s.slot == 0)
        .unwrap()
        .detail
        .contains("CrcMismatch"));
}

// ---------------------------------------------------------------------------
// Both roots corrupt -> refused
// ---------------------------------------------------------------------------

#[test]
fn both_superblocks_corrupt_is_refused() {
    let mut sim = commit_n(SimVfs::fresh(no_crash()), 3);
    for id in [FileId::SbA, FileId::SbB] {
        let mut page = sim.durable_bytes(id).unwrap();
        // half-zero: header half present, rest gone -> CRC mismatch
        for b in page.iter_mut().skip(SB_SIZE / 2) {
            *b = 0;
        }
        sim.corrupt_durable(id, 0, &page);
    }
    let err = Repository::open(sim.reopen(no_crash())).err();
    assert!(matches!(err, Some(StoreError::NoValidSuperblock)), "{err:?}");
}

#[test]
fn single_bit_flip_in_both_roots_is_refused() {
    let mut sim = commit_n(SimVfs::fresh(no_crash()), 2);
    for id in [FileId::SbA, FileId::SbB] {
        let mut page = sim.durable_bytes(id).unwrap();
        page[500] ^= 0x01;
        sim.corrupt_durable(id, 0, &page);
    }
    let err = Repository::open(sim.reopen(no_crash())).err();
    assert!(matches!(err, Some(StoreError::NoValidSuperblock)), "{err:?}");
}

// ---------------------------------------------------------------------------
// Old-root rollback: newer gen with invalid references
// ---------------------------------------------------------------------------

#[test]
fn higher_generation_oob_pointer_is_rejected_old_root_wins() {
    let mut sim = commit_n(SimVfs::fresh(no_crash()), 2);
    let data_len = sim.durable_len(FileId::Data);
    // Well-formed CRC, absurd generation, pointer past EOF.
    let forged = Superblock {
        generation: 42,
        data_len: data_len + 9999,
        root_off: data_len,
        slot: 0,
    };
    sim.corrupt_durable(FileId::SbA, 0, &forged.encode());

    let reboot = sim.reopen(no_crash());
    let (repo, report) = Repository::open(reboot).unwrap();
    assert_eq!(repo.generation(), 2, "OOB gen-42 must never be selected");
    assert_eq!(report.selected_generation, Some(2));
    let a = report.slots.iter().find(|s| s.slot == 0).unwrap();
    assert!(!a.accepted);
    assert!(a.detail.contains("OutOfBounds"), "{}", a.detail);
}

#[test]
fn higher_generation_chain_crc_corrupt_falls_back() {
    // Newer root points at a record whose header CRC is wrong.
    let mut sim = commit_n(SimVfs::fresh(no_crash()), 1);
    let data_len = sim.durable_len(FileId::Data);

    // Hand-craft a "gen 5" record at the tail, with a corrupted header,
    // then point a valid-CRC superblock at it.
    let payload = Op::encode_put(b"k", b"evil");
    let mut rec = RecordHeader::encode(data_len as u32, 5, data_len, &payload);
    rec[15] ^= 0xFF; // smash generation bytes -> header CRC fails
    sim.corrupt_durable(FileId::Data, data_len, &rec);
    let forged = Superblock {
        generation: 5,
        data_len: data_len + rec.len() as u64,
        root_off: data_len,
        slot: 1, // gen 1 root is in sb.a; put the liar in sb.b
    };
    sim.corrupt_durable(FileId::SbB, 0, &forged.encode());

    let (repo, report) = Repository::open(sim.reopen(no_crash())).unwrap();
    assert_eq!(repo.generation(), 1);
    assert_eq!(repo.get(b"k"), Some(b"v1".as_slice()));
    let b = report.slots.iter().find(|s| s.slot == 1).unwrap();
    assert!(!b.accepted);
    assert!(b.detail.contains("CrcMismatch"), "{}", b.detail);
}

// ---------------------------------------------------------------------------
// Torn data append: half record on media must be truncated, root kept
// ---------------------------------------------------------------------------

#[test]
fn torn_data_record_is_truncated_on_recovery() {
    // Seed gens 1..2, then crash at RootWriteBegin (data already synced)
    // with a half-torn write: nothing tears at that point, but to actually
    // exercise a torn data tail we instead crash at DataSyncBegin with Half,
    // which lands a partial gen-3 record at the file tail.
    let mut sim = SimVfs::fresh(no_crash());
    for g in 1u64..=2 {
        let (mut repo, _) = Repository::open(sim).unwrap();
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        sim = repo.into_vfs();
    }
    let g2_len = sim.durable_len(FileId::Data);
    let (mut repo, _) = Repository::open(sim).unwrap();
    repo.arm_crash_policy(CrashPolicy::new(CrashPoint::DataSyncBegin, Torn::Half));
    let res = repo.put(b"k".to_vec(), b"v3".to_vec());
    assert!(matches!(res, Err(StoreError::Io(IoError::SimulatedCrash))));
    let sim = repo.into_vfs();

    // A partial gen-3 record sits durable beyond the gen-2 high-water mark.
    let media_len = sim.durable_len(FileId::Data);
    assert!(media_len > g2_len, "torn prefix must be on media");
    let (repo, report) = Repository::open(sim.reopen(no_crash())).unwrap();
    assert_eq!(repo.generation(), 2);
    assert_eq!(report.truncated_orphan_bytes, media_len - g2_len);
    assert!(report.truncated_orphan_bytes > 0);
    // After recovery the media itself was trimmed to the validated prefix.
    let vfs = repo.into_vfs();
    assert_eq!(vfs.durable_len(FileId::Data), g2_len);
}

#[test]
fn dirty_writes_lost_when_crash_before_any_sync() {
    // Torn::None at DataWriteEnd: pwrite returned but never fsynced; after
    // reboot the data file is exactly as gen 2 left it.
    let mut sim = SimVfs::fresh(no_crash());
    for g in 1u64..=2 {
        let (mut repo, _) = Repository::open(sim).unwrap();
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        sim = repo.into_vfs();
    }
    let g2_len = sim.durable_len(FileId::Data);
    let (mut repo, _) = Repository::open(sim).unwrap();
    repo.arm_crash_policy(CrashPolicy::new(CrashPoint::DataWriteEnd, Torn::None));
    let res = repo.put(b"k".to_vec(), b"v3".to_vec());
    assert!(matches!(res, Err(StoreError::Io(IoError::SimulatedCrash))));
    let sim = repo.into_vfs();
    assert_eq!(sim.durable_len(FileId::Data), g2_len);
    let (repo, _) = Repository::open(sim.reopen(no_crash())).unwrap();
    assert_eq!(repo.generation(), 2);
}

// ---------------------------------------------------------------------------
// Alternation invariants on a clean multi-generation history
// ---------------------------------------------------------------------------

#[test]
fn slots_alternate_and_generations_monotonic() {
    let sim = commit_n(SimVfs::fresh(no_crash()), 6);
    let a = Superblock::decode(&sim.durable_bytes(FileId::SbA).unwrap(), u64::MAX).unwrap();
    let b = Superblock::decode(&sim.durable_bytes(FileId::SbB).unwrap(), u64::MAX).unwrap();
    // gens 1,3,5 -> slot A; gens 2,4,6 -> slot B; B is newest.
    assert_eq!((a.generation, a.slot), (5, 0));
    assert_eq!((b.generation, b.slot), (6, 1));
    let (repo, report) = Repository::open(sim.reopen(no_crash())).unwrap();
    assert_eq!(repo.generation(), 6);
    assert_eq!(report.selected_slot, Some(1));
}

#[test]
fn dirty_page_cache_is_cold_after_reboot() {
    let mut v = SimVfs::fresh(CrashPolicy::new(CrashPoint::RootWriteEnd, Torn::None));
    let _ = v.pwrite(FileId::SbA, 0, &[1u8; SB_SIZE]).unwrap_err();
    // nothing durable
    assert_eq!(v.durable_len(FileId::SbA), 0);
    let reboot = v.reopen(no_crash());
    assert_eq!(reboot.durable_len(FileId::SbA), 0);
}
