//! File-backed append-only key/value store with alternating dual
//! superblocks and crash recovery.
//!
//! ## Sync boundary / commit protocol
//!
//! One commit appends exactly one data record, then publishes a new
//! superblock pointing at it:
//!
//! ```text
//! 1. pwrite(data.log, tail, record)      // new data sits in the page cache
//! 2. fsync(data.log)                     // BARRIER: record is durable
//! 3. pwrite(slot, 0, superblock(page))   // root points at the durable record
//! 4. fsync(slot)                         // new root is durable -> visible
//! ```
//!
//! The crucial ordering is 2 before 3: a root is never written until the
//! bytes it references are already on disk. Any crash in steps 1–2 leaves
//! the old root intact pointing at the old chain; a crash in steps 3–4 tears
//! the new page (its CRC fails) so the *other*, older slot still recovers.
//!
//! ## Recovery selection
//!
//! Both 4 KiB superblock pages are decoded and CRC-checked. Every page is
//! additionally required to reference a valid, fully present record chain:
//!
//! * root/data pointers inside the file (`OutOfBounds` otherwise);
//! * records contiguous and packed from offset 0;
//! * every header CRC valid;
//! * head record carries the superblock generation, first record at offset 0
//!   with `prev_off == NIL_PREV`.
//!
//! Among valid candidates the **highest generation** wins; ties break on the
//! higher slot (slot B was written second in a same-generation tie). Invalid
//! pages are skipped, so a torn newer page transparently falls back to the
//! older one. Bytes past the selected chain end are a torn/orphan tail and
//! are truncated away on open.

use std::collections::BTreeMap;

use crate::format::{self, Op, RecordHeader, Superblock, SB_SIZE};
use crate::io::{FileId, IoError, Vfs};

/// In-memory replayed key/value map.
pub type KvMap = BTreeMap<Vec<u8>, Vec<u8>>;

#[derive(Debug)]
pub enum StoreError {
    Io(IoError),
    Format(format::FormatError),
    /// Both non-empty superblock pages are present but invalid.
    NoValidSuperblock,
    /// A (claimed valid) superblock references an invalid record chain.
    CorruptChain(String),
    KeyNotFound,
    Other(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "{e}"),
            StoreError::Format(e) => write!(f, "format error: {e}"),
            StoreError::NoValidSuperblock => write!(f,
                "both superblocks are present but invalid (cannot recover)"),
            StoreError::CorruptChain(s) => write!(f, "corrupt record chain: {s}"),
            StoreError::KeyNotFound => write!(f, "key not found"),
            StoreError::Other(s) => write!(f, "{s}"),
        }
    }
}
impl std::error::Error for StoreError {}

impl From<IoError> for StoreError {
    fn from(e: IoError) -> Self {
        StoreError::Io(e)
    }
}
impl From<format::FormatError> for StoreError {
    fn from(e: format::FormatError) -> Self {
        StoreError::Format(e)
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;

/// Why each superblock slot was accepted or rejected during recovery.
#[derive(Debug, Clone)]
pub struct SlotReport {
    pub slot: u32,
    pub present: bool,
    pub accepted: bool,
    pub detail: String,
    pub generation: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct RecoveryReport {
    pub slots: Vec<SlotReport>,
    pub selected_slot: Option<u32>,
    pub selected_generation: Option<u64>,
    pub data_len_before: u64,
    pub data_len_after: u64,
    pub truncated_orphan_bytes: u64,
}

pub struct Repository<V: Vfs> {
    vfs: V,
    kv: KvMap,
    /// Generation of the currently published root.
    generation: u64,
    /// Slot holding the currently published root.
    active_slot: u32,
    /// Logical (validated) length of data.log.
    data_len: u64,
    /// Offset of the head record of the current chain.
    root_off: u64,
}

impl<V: Vfs> Repository<V> {
    /// Open and recover. On a fresh medium (`data.log` absent and neither
    /// slot non-empty) returns an empty store at generation 0; the first
    /// commit creates the files.
    pub fn open(mut vfs: V) -> Result<(Self, RecoveryReport)> {
        let data = vfs.read(FileId::Data)?;
        let file_len = data.as_ref().map(|d| d.len() as u64).unwrap_or(0);
        let data = data.unwrap_or_default();

        let mut slots = Vec::new();
        let mut candidates: Vec<(Superblock, String)> = Vec::new();

        for slot in 0u32..2 {
            let id = [FileId::SbA, FileId::SbB][slot as usize];
            let raw = vfs.read(id)?;
            let report = match raw {
                None => SlotReport {
                    slot,
                    present: false,
                    accepted: false,
                    detail: "absent".to_string(),
                    generation: None,
                },
                Some(page) => {
                    let present_nonempty = page.iter().any(|&b| b != 0);
                    match Superblock::decode(&page, file_len) {
                        Ok(sb) => match validate_chain(&data, file_len, &sb) {
                            Ok(detail) => {
                                let gen = sb.generation;
                                candidates.push((sb, detail.clone()));
                                SlotReport {
                                    slot,
                                    present: true,
                                    accepted: true,
                                    detail,
                                    generation: Some(gen),
                                }
                            }
                            Err(e) => SlotReport {
                                slot,
                                present: true,
                                accepted: false,
                                detail: format!("superblock valid but chain rejected: {e}"),
                                generation: None,
                            },
                        },
                        Err(e) => SlotReport {
                            slot,
                            present: present_nonempty,
                            accepted: false,
                            detail: format!("rejected: {e}"),
                            generation: None,
                        },
                    }
                }
            };
            slots.push(report);
        }

        let any_present = slots.iter().any(|s| s.present);
        let selected = candidates
            .into_iter()
            .max_by(|a, b| {
                a.0.generation
                    .cmp(&b.0.generation)
                    .then(a.0.slot.cmp(&b.0.slot))
            });

        let mut report = RecoveryReport {
            slots,
            selected_slot: None,
            selected_generation: None,
            data_len_before: file_len,
            data_len_after: file_len,
            truncated_orphan_bytes: 0,
        };

        let (kv, generation, active_slot, data_len, root_off) = match selected {
            Some((sb, _)) => {
                let (replayed, valid_len, root) = replay(&data, file_len, &sb)?;
                if valid_len < file_len {
                    // Torn/orphan tail from a crash during data write or from
                    // garbage: discard it so future appends stay packed.
                    vfs.truncate(FileId::Data, valid_len)?;
                    vfs.sync(FileId::Data)?;
                }
                report.selected_slot = Some(sb.slot);
                report.selected_generation = Some(sb.generation);
                report.data_len_after = valid_len;
                report.truncated_orphan_bytes = file_len - valid_len;
                (replayed, sb.generation, sb.slot, valid_len, root)
            }
            None => {
                if any_present {
                    return Err(StoreError::NoValidSuperblock);
                }
                // Genuinely fresh repository.
                (BTreeMap::new(), 0, 1, 0, 0)
            }
        };

        Ok((
            Repository {
                vfs,
                kv,
                generation,
                active_slot,
                data_len,
                root_off,
            },
            report,
        ))
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn data_len(&self) -> u64 {
        self.data_len
    }
    pub fn entries(&self) -> Vec<(String, String)> {
        self.kv
            .iter()
            .map(|(k, v)| {
                (
                    String::from_utf8_lossy(k).into_owned(),
                    String::from_utf8_lossy(v).into_owned(),
                )
            })
            .collect()
    }

    pub fn get(&self, key: &[u8]) -> Option<&[u8]> {
        self.kv.get(key).map(|v| v.as_slice())
    }

    /// Atomically (per the crash-safety protocol) publish one batch.
    /// All entries are put; keys paired with `None` are deleted.
    pub fn commit(&mut self, batch: Vec<(Vec<u8>, Option<Vec<u8>>)>) -> Result<u64> {
        let mut payload = Vec::new();
        for (key, value) in batch {
            match value {
                Some(v) => payload.extend_from_slice(&Op::encode_put(&key, &v)),
                None => payload.extend_from_slice(&Op::encode_delete(&key)),
            }
        }
        // Every commit carries at least one operation: keeps generation and
        // record count identical and chains non-empty.
        assert!(!payload.is_empty(), "empty commit");

        let new_generation = self.generation + 1;
        let prev_off = if self.generation == 0 {
            format::NIL_PREV
        } else {
            self.root_off as u32
        };
        let record_off = self.data_len;
        let record = RecordHeader::encode(prev_off, new_generation, record_off, &payload);

        // Step 1: append data (dirty until the barrier).
        self.vfs.pwrite(FileId::Data, record_off, &record)?;
        // Step 2: durability barrier for the data.
        self.vfs.sync(FileId::Data)?;

        // Step 3: publish the new root into the alternating slot.
        let new_slot = if self.generation == 0 { 0 } else { self.active_slot ^ 1 };
        let new_data_len = record_off + record.len() as u64;
        let sb = Superblock {
            generation: new_generation,
            data_len: new_data_len,
            root_off: record_off,
            slot: new_slot,
        };
        let page = sb.encode();
        debug_assert_eq!(page.len(), SB_SIZE);
        let id = if new_slot == 0 { FileId::SbA } else { FileId::SbB };
        self.vfs.pwrite(id, 0, &page)?;
        // On the very first commit the files themselves were just created;
        // persist their directory entries before the root barrier.
        if self.generation == 0 {
            self.vfs.sync_dir()?;
        }
        // Step 4: root durability barrier — commit becomes visible here.
        self.vfs.sync(id)?;

        // Publish in-memory state only after the media commit succeeded.
        for op in payload_to_ops(&payload)? {
            match op {
                Op::Put(k, v) => {
                    self.kv.insert(k, v);
                }
                Op::Delete(k) => {
                    self.kv.remove(&k);
                }
            }
        }

        self.generation = new_generation;
        self.active_slot = new_slot;
        self.data_len = new_data_len;
        self.root_off = record_off;
        Ok(new_generation)
    }

    pub fn put(&mut self, key: Vec<u8>, value: Vec<u8>) -> Result<u64> {
        self.commit(vec![(key, Some(value))])
    }

    pub fn delete(&mut self, key: Vec<u8>) -> Result<u64> {
        self.commit(vec![(key, None)])
    }

    /// Recover over an existing VFS without the open-time truncation path
    /// being affected; this is the same as [`Repository::open`] but lets
    /// callers keep ownership of the media wrapper.
    pub fn recover(vfs: V) -> Result<(Self, RecoveryReport)> {
        Self::open(vfs)
    }

    /// Consume the repository and return its storage backend (e.g. to reboot
    /// a [`crate::io::SimVfs`] after a simulated crash).
    pub fn into_vfs(self) -> V {
        self.vfs
    }

    /// Arm a fault-injection policy on an injectable backend. Only the
    /// in-memory simulator supports this; used by the crash demo harness.
    pub fn arm_crash_policy(&mut self, policy: crate::io::CrashPolicy)
    where
        V: CrashArmable,
    {
        self.vfs.set_crash_policy(policy);
    }
}

/// Backends that can accept a crash policy (the fault-injecting simulator).
pub trait CrashArmable {
    fn set_crash_policy(&mut self, policy: crate::io::CrashPolicy);
}

impl CrashArmable for crate::io::SimVfs {
    fn set_crash_policy(&mut self, policy: crate::io::CrashPolicy) {
        self.set_policy(policy);
    }
}

fn payload_to_ops(payload: &[u8]) -> Result<Vec<Op>> {
    // Payload is a concatenation of encoded Ops from this commit; split it.
    let mut ops = Vec::new();
    let mut rest = payload;
    while !rest.is_empty() {
        if rest.len() < 9 {
            return Err(StoreError::CorruptChain("short payload".into()));
        }
        let klen = u32::from_le_bytes(rest[1..5].try_into().unwrap()) as usize;
        let vlen = u32::from_le_bytes(rest[5..9].try_into().unwrap()) as usize;
        let n = match rest[0] {
            format::OP_PUT => 9 + klen + vlen,
            format::OP_DELETE => 9 + klen,
            _ => return Err(format::FormatError::BadPayload.into()),
        };
        if n > rest.len() {
            return Err(format::FormatError::BadPayload.into());
        }
        ops.push(Op::decode(&rest[..n])?);
        rest = &rest[n..];
    }
    Ok(ops)
}

/// Validate the record chain referenced by a decoded superblock and return
/// `(replayed map, validated data length, head offset)`.
fn replay(
    data: &[u8],
    file_len: u64,
    sb: &Superblock,
) -> Result<(KvMap, u64, u64)> {
    let mut offs = Vec::new();
    let mut cur = sb.root_off;
    let mut is_head = true;
    loop {
        let (h, _payload) = RecordHeader::parse_at(data, sb.data_len, cur)
            .map_err(|e| StoreError::CorruptChain(format!("record at {cur}: {e}")))?;
        if is_head {
            // Only the head record must carry the superblock's generation;
            // older records legitimately keep the generation they were
            // written under.
            if h.generation != sb.generation {
                return Err(StoreError::CorruptChain(format!(
                    "head record at {cur} generation {} != root generation {}",
                    h.generation, sb.generation
                )));
            }
            is_head = false;
        }
        offs.push(cur);
        if cur == 0 {
            // Walked back to the first packed record, which always sits at
            // offset 0. The first record's prev_off is the NIL sentinel.
            if h.prev_off != format::NIL_PREV {
                return Err(StoreError::CorruptChain(format!(
                    "record at offset 0 must carry NIL prev_off, got {}",
                    h.prev_off
                )));
            }
            break;
        }
        cur = h.prev_off as u64;
    }
    // offs runs head -> oldest. Replay oldest -> newest.
    offs.reverse();

    // Contiguity: packed records starting at offset 0.
    let mut expect = 0u64;
    let mut kv = BTreeMap::new();
    for &off in &offs {
        if off != expect {
            return Err(StoreError::CorruptChain(format!(
                "non-contiguous chain: record at {off}, expected {expect}"
            )));
        }
        let (h, payload) = RecordHeader::parse_at(data, sb.data_len, off).unwrap();
        match Op::decode(payload) {
            Ok(Op::Put(k, v)) => {
                kv.insert(k, v);
            }
            Ok(Op::Delete(k)) => {
                kv.remove(&k);
            }
            Err(e) => {
                return Err(StoreError::CorruptChain(format!(
                    "record at {off}: bad payload: {e}"
                )))
            }
        }
        expect = off + RecordHeader::total_len(h.payload_len);
    }

    // The validated prefix reaches exactly the end the superblock claims.
    if expect != sb.data_len {
        return Err(StoreError::CorruptChain(format!(
            "chain ends at {expect} but superblock claims data_len {}",
            sb.data_len
        )));
    }
    // It may be shorter than the physical file (orphan tail); the caller
    // truncates. It can never be longer (decode already bounded reads).
    let _ = file_len;
    Ok((kv, expect, sb.root_off))
}

/// Lighter check used during candidate selection: same validation but only
/// the detail string is returned.
fn validate_chain(data: &[u8], file_len: u64, sb: &Superblock) -> std::result::Result<String, String> {
    match replay(data, file_len, sb) {
        Ok((_kv, valid_len, _root)) => {
            let orphan = file_len.saturating_sub(valid_len);
            Ok(format!(
                "valid chain, generation {}, {} bytes, {orphan} orphan tail byte(s)",
                sb.generation, valid_len
            ))
        }
        Err(e) => Err(e.to_string()),
    }
}
