//! Deterministic crash-scenario harness, reusable by the CLI (`demo`) and
//! the HTTP validation endpoints (`/demo/...`).
//!
//! Every scenario runs against the in-memory [`SimVfs`]: the crash policy is
//! armed, a commit is attempted, `SimulatedCrash` is swallowed (that is the
//! point), the media is "rebooted" and the repository recovered. What gets
//! asserted is the recovered generation/key state and the truncation of
//! orphan bytes — not the crash itself.

use crate::format::{Superblock, SB_SIZE};
use crate::io::{CrashPoint, CrashPolicy, FileId, IoError, SimVfs, Torn};
use crate::json::Json;
use crate::repo::{Repository, StoreError};

pub fn parse_point(s: &str) -> Option<CrashPoint> {
    use CrashPoint::*;
    Some(match s {
        "DataWriteBegin" => DataWriteBegin,
        "DataWriteEnd" => DataWriteEnd,
        "DataSyncBegin" => DataSyncBegin,
        "DataSyncEnd" => DataSyncEnd,
        "RootWriteBegin" => RootWriteBegin,
        "RootWriteEnd" => RootWriteEnd,
        "RootSyncBegin" => RootSyncBegin,
        "RootSyncEnd" => RootSyncEnd,
        _ => return None,
    })
}

pub fn point_name(p: CrashPoint) -> &'static str {
    use CrashPoint::*;
    match p {
        DataWriteBegin => "DataWriteBegin",
        DataWriteEnd => "DataWriteEnd",
        DataSyncBegin => "DataSyncBegin",
        DataSyncEnd => "DataSyncEnd",
        RootWriteBegin => "RootWriteBegin",
        RootWriteEnd => "RootWriteEnd",
        RootSyncBegin => "RootSyncBegin",
        RootSyncEnd => "RootSyncEnd",
    }
}

pub fn parse_torn(s: &str) -> Option<Torn> {
    Some(match s {
        "None" => Torn::None,
        "Half" => Torn::Half,
        "Short" => Torn::Short,
        _ => return None,
    })
}

pub fn torn_name(t: Torn) -> &'static str {
    match t {
        Torn::None => "None",
        Torn::Half => "Half",
        Torn::Short => "Short",
    }
}

/// A policy that never fires (used for clean seed commits and recovery).
fn no_crash() -> CrashPolicy {
    CrashPolicy::never()
}

#[derive(Debug, Clone)]
pub struct CaseResult {
    pub point: &'static str,
    pub torn: &'static str,
    pub crashed_round: u64,
    pub opened: bool,
    pub recovered_generation: Option<u64>,
    pub expected_generation: u64,
    pub last_value: Option<String>,
    pub orphan_bytes_truncated: u64,
    /// (slot, present, accepted, detail)
    pub slot_reports: Vec<(u32, bool, bool, String)>,
    pub error: Option<String>,
}

impl CaseResult {
    /// Acceptance predicate used by both the CLI and the tests.
    pub fn passed(&self) -> bool {
        let want = format!("v{}", self.expected_generation);
        self.opened
            && self.error.is_none()
            && self.recovered_generation == Some(self.expected_generation)
            && self.last_value.as_deref() == Some(want.as_str())
    }
}

/// Run one commit-crash-recovery case.
///
/// * `rounds` — crash is injected while publishing generation `rounds`;
/// * key `k` is written as `v1..v{rounds}`; the crashing write is `vC`.
///
/// Recovery must select the highest *complete, valid* generation:
/// generation `rounds` when the crash point is at/past the root barrier,
/// otherwise `rounds - 1`.
/// Seed `rounds-1` clean commits, open the final repository, arm the crash
/// policy *after* open (so recovery's own truncate/sync never triggers it)
/// and attempt the crashing commit. Returns the post-crash media.
pub fn crash_media(policy: CrashPolicy, rounds: u64) -> SimVfs {
    let mut sim = SimVfs::fresh(CrashPolicy::never());
    for g in 1..rounds {
        let (mut repo, _) = Repository::open(sim).expect("open seed commit");
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes())
            .expect("seed commit must succeed");
        sim = repo.into_vfs();
    }
    let (mut repo, _) = Repository::open(sim).expect("open before crash commit");
    if policy.at.is_some() {
        repo.arm_crash_policy(policy);
    }
    let res = repo.put(b"k".to_vec(), format!("v{rounds}").into_bytes());
    let sim = repo.into_vfs();
    match res {
        Ok(_) => {}
        Err(StoreError::Io(IoError::SimulatedCrash)) => {}
        Err(e) => panic!("unexpected error on crash round {rounds}: {e}"),
    }
    sim
}

pub fn run_single(policy: CrashPolicy, rounds: u64) -> CaseResult {
    let point = policy.at.unwrap_or(CrashPoint::RootSyncEnd);
    let torn = policy.torn;
    // Only a crash that lands at/after the final root barrier is a
    // committed write that survives the reboot.
    let committed = policy.at == Some(CrashPoint::RootSyncEnd);
    let expected = if committed { rounds } else { rounds - 1 };

    let sim = crash_media(policy, rounds);

    // --- reboot and recover ---
    let reboot = sim.reopen(no_crash());
    let open = Repository::open(reboot);
    match open {
        Ok((repo, report)) => {
            let last = repo
                .get(b"k")
                .map(|v| String::from_utf8_lossy(v).into_owned());
            let slots = report
                .slots
                .iter()
                .map(|s| (s.slot, s.present, s.accepted, s.detail.clone()))
                .collect();
            CaseResult {
                point: point_name(point),
                torn: torn_name(torn),
                crashed_round: rounds,
                opened: true,
                recovered_generation: Some(repo.generation()),
                expected_generation: expected,
                last_value: last,
                orphan_bytes_truncated: report.truncated_orphan_bytes,
                slot_reports: slots,
                error: None,
            }
        }
        Err(e) => CaseResult {
            point: point_name(point),
            torn: torn_name(torn),
            crashed_round: rounds,
            opened: false,
            recovered_generation: None,
            expected_generation: expected,
            last_value: None,
            orphan_bytes_truncated: 0,
            slot_reports: Vec::new(),
            error: Some(e.to_string()),
        },
    }
}

/// 8 points × 3 torn variants, crash during commit 3.
pub fn run_matrix() -> Vec<CaseResult> {
    let mut out = Vec::new();
    for point in CrashPoint::ALL {
        for torn in [Torn::None, Torn::Half, Torn::Short] {
            out.push(run_single(CrashPolicy::new(point, torn), 3));
        }
    }
    out
}

/// Every point with a half-page torn write, crash during commit 2 (the
/// first rollback, where the older slot is generation 1).
pub fn run_round2_matrix() -> Vec<CaseResult> {
    CrashPoint::ALL
        .iter()
        .map(|&point| run_single(CrashPolicy::new(point, Torn::Half), 2))
        .collect()
}

// ---------------------------------------------------------------------------
// The three named acceptance scenarios requested for the delivery.
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct NamedScenario {
    pub name: String,
    pub description: String,
    pub outcome: String,
    pub report: Json,
}


fn json_slots(sim: &SimVfs) -> Json {
    let file_len = sim.durable_len(FileId::Data);
    let mut arr = Vec::new();
    for slot in 0u32..2 {
        let id = if slot == 0 { FileId::SbA } else { FileId::SbB };
        let raw = sim.durable_bytes(id);
        let (entry, page_len) = match raw {
            None => (Json::Arr(vec![Json::s("absent")]), 0),
            Some(page) => {
                let page_len = page.len() as u64;
                let mut bits = Vec::new();
                let present = page.iter().any(|&b| b != 0);
                if !present {
                    bits.push(Json::s("present but all-zero"));
                }
                match Superblock::decode(&page, file_len) {
                    Ok(sb) => {
                        bits.push(Json::s(format!(
                            "valid gen={} data_len={} root_off={} slot={}",
                            sb.generation, sb.data_len, sb.root_off, sb.slot
                        )));
                    }
                    Err(e) => bits.push(Json::s(format!("invalid: {e}"))),
                }
                (Json::Arr(bits), page_len)
            }
        };
        arr.push(Json::Obj(vec![
            ("slot".into(), Json::n(slot as u64)),
            ("page_bytes".into(), Json::n(page_len)),
            ("data_file_bytes".into(), Json::n(file_len)),
            ("status".into(), entry),
        ]));
    }
    Json::Arr(arr)
}

/// Scenario 1: half-page write of the *new* superblock (RootWriteEnd with
/// Torn::Half). New page CRC fails → recovery falls back to the older slot
/// (generation 2), v3 is absent.
pub fn scenario_half_page_root() -> NamedScenario {
    let policy = CrashPolicy::new(CrashPoint::RootWriteEnd, Torn::Half);
    let case = run_single(policy, 3);
    let outcome = format!(
        "recovered gen {} (expected 2), value {:?}, passed={}",
        case.recovered_generation.unwrap_or(0),
        case.last_value,
        case.passed()
    );
    // Build the actual post-crash media once for the report.
    let sim = crash_media(policy, 3);
    let reboot = sim.reopen(no_crash());
    NamedScenario {
        name: "half-page-superblock-write".into(),
        description: "crash after a torn half-page pwrite of the new \
            superblock (generation 3); CRC of the partial page must fail"
            .into(),
        outcome,
        report: Json::Obj(vec![
            ("torn_superblock_pages_on_reboot".into(), json_slots(&reboot)),
            ("selected_generation".into(), Json::n(2)),
            ("v3_visible".into(), Json::Bool(false)),
        ]),
    }
}

/// Scenario 2: both roots corrupt. After committing generation 3, overwrite
/// both superblock slots with a torn (half zeroed) page. Recovery must fail
/// with `NoValidSuperblock` rather than accept a bad root.
pub fn scenario_both_roots_corrupt() -> NamedScenario {
    let mut sim = SimVfs::fresh(no_crash());
    for g in 1u64..=3 {
        let (mut repo, _) = Repository::open(sim).unwrap();
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        sim = repo.into_vfs();
    }
    // Tear both pages: keep the first half (which contains the header) and
    // zero the second half → CRC fails on each.
    for id in [FileId::SbA, FileId::SbB] {
        let mut page = sim.durable_bytes(id).unwrap();
        for b in page.iter_mut().skip(SB_SIZE / 2) {
            *b = 0;
        }
        sim.corrupt_durable(id, 0, &page);
    }
    // Snapshot the pages for the report before recovery consumes the VFS.
    let inspected = sim.reopen_cloned(no_crash());
    let slots_json = json_slots(&inspected);
    let err = Repository::open(sim.reopen(no_crash())).err();
    let outcome = match &err {
        Some(StoreError::NoValidSuperblock) => {
            "open refused: both superblocks invalid (as required)".into()
        }
        Some(other) => format!("UNEXPECTED: opened or failed wrong: {other}"),
        None => "UNEXPECTED: repository opened despite both roots corrupt".into(),
    };
    NamedScenario {
        name: "both-roots-corrupt".into(),
        description: "generation 3 committed, then both superblock pages are \
            half-zeroed (CRC fails); recovery must refuse, never pick a bad root"
            .into(),
        outcome,
        report: Json::Obj(vec![
            ("superblock_pages".into(), slots_json),
            (
                "error".into(),
                Json::s(err.as_ref().map(|e| e.to_string()).unwrap_or_default()),
            ),
            ("correctly_refused".into(), Json::Bool(
                matches!(err, Some(StoreError::NoValidSuperblock)))),
        ]),
    }
}

/// Scenario 3: stale-root rollback. The newer slot carries a *well-formed*
/// page whose pointers are out of bounds (`data_len` beyond the file); the
/// older slot is generation 2 and valid. Recovery must reject the newer
/// pointer and select gen 2.
pub fn scenario_stale_oob_root() -> NamedScenario {
    let mut sim = SimVfs::fresh(no_crash());
    for g in 1u64..=2 {
        let mut repo = Repository::open(sim).unwrap().0;
        repo.put(b"k".to_vec(), format!("v{g}").into_bytes()).unwrap();
        sim = repo.into_vfs();
    }
    let data_len = sim.durable_len(FileId::Data);
    // After gen 2 the active root is slot B (1). Forge a higher-generation
    // page into the *other* slot (A) with an out-of-bounds data_len, so the
    // newest-looking pointer lies and recovery must fall back to gen 2.
    let forged = Superblock {
        generation: 99,
        data_len: data_len + 4096, // past end of file
        root_off: data_len,
        slot: 0,
    };
    sim.corrupt_durable(FileId::SbA, 0, &forged.encode());

    let outcome;
    let gen;
    let val;
    match Repository::open(sim.reopen(no_crash())) {
        Ok((repo, report)) => {
            gen = Some(repo.generation());
            val = repo
                .get(b"k")
                .map(|v| String::from_utf8_lossy(v).into_owned());
            let a_detail = report
                .slots
                .iter()
                .find(|s| s.slot == 0)
                .map(|s| s.detail.clone())
                .unwrap_or_default();
            outcome = format!(
                "recovered gen {} (expected 2), value {:?}; gen-99 slot rejected: {a_detail}",
                gen.unwrap_or(0),
                val
            );
        }
        Err(e) => {
            outcome = format!("UNEXPECTED failure: {e}");
            gen = None;
            val = None;
        }
    };
    NamedScenario {
        name: "out-of-bounds-newer-root-rollback".into(),
        description: "newer slot has higher generation but data_len points \
            past end of file; the OOB pointer must be rejected and the valid \
            older gen-2 root selected".into(),
        outcome,
        report: Json::Obj(vec![
            ("forged_generation".into(), Json::n(99)),
            ("selected_generation".into(), Json::n(gen.unwrap_or(0))),
            ("value".into(), Json::s(val.unwrap_or_default())),
        ]),
    }
}

pub fn all_named() -> Vec<NamedScenario> {
    vec![
        scenario_half_page_root(),
        scenario_both_roots_corrupt(),
        scenario_stale_oob_root(),
    ]
}
