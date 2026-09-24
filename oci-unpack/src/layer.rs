//! Layer verification and application — the heart of the unpacker.
//!
//! Each layer blob is processed in two independent streaming passes:
//!
//! 1. [`scan_verify`] — no filesystem mutation. Opens the blob, hashes every
//!    compressed byte, inflates (gzip) if required, walks every tar header,
//!    validates every path/link target, and drains each payload through the
//!    limit counters. The computed digest must match the manifest descriptor
//!    or the whole rebuild aborts before anything is published.
//!
//! 2. [`scan_apply`] — replays the same stream and materialises entries into
//!    the staging rootfs. It re-hashes and re-asserts the digest.
//!
//! OCI whiteout semantics are order-independent: a `.wh.` / opaque marker only
//! ever hides *lower-layer* content. An `in_layer` set records what the current
//! tar has already created, so a marker appearing after same-layer files does
//! not remove them regardless of archive member order.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

use tar::{Archive, EntryType};

use crate::error::{Error, Result};
use crate::fsops;
use crate::io_util::{drain_entry, gzip_decoder, HashingReader, MeteringReader};
use crate::limits::{Limits, Usage};
use crate::path::{
    assert_no_symlink_ancestor, normalize_entry_name, normalize_link_target,
    validate_symlink_target, RelPath,
};

pub const WHITEOUT_PREFIX: &str = ".wh.";
pub const OPAQUE_WHITEOUT: &str = ".wh..wh..opq";

/// Accumulated rootfs state passed from layer to layer.
#[derive(Debug, Default)]
pub struct RootfsState {
    /// Surviving path → (layer index, layer digest, kind).
    pub provenance: BTreeMap<String, (usize, String, &'static str)>,
    /// Paths that are currently symlinks (for ancestor checks).
    pub symlinks: BTreeSet<String>,
}

#[derive(Debug, Clone)]
pub struct ProvenanceEntry {
    pub path: String,
    pub kind: &'static str,
    pub layer_index: usize,
    pub layer_digest: String,
}

impl RootfsState {
    pub fn entries(&self) -> Vec<ProvenanceEntry> {
        self.provenance
            .iter()
            .map(|(path, (idx, digest, kind))| ProvenanceEntry {
                path: path.clone(),
                kind,
                layer_index: *idx,
                layer_digest: digest.clone(),
            })
            .collect()
    }
}

/// Per-layer statistics.
#[derive(Debug, Default, Clone)]
pub struct LayerReport {
    pub index: usize,
    pub digest: String,
    pub media_type: String,
    pub compressed_size: u64,
    pub decompressed_size: u64,
    pub payload_bytes: u64,
    pub entries_seen: u64,
    pub files: u64,
    pub dirs: u64,
    pub symlinks: u64,
    pub hardlinks: u64,
    pub whiteouts: u64,
    pub opaque_dirs: u64,
}

/// What the scanner needs from a generic reader chain.
struct StreamStats {
    /// SHA-256 of the on-disk (possibly compressed) blob.
    digest: String,
    /// SHA-256 of the uncompressed tar (OCI `diff_id`).
    diff_digest: String,
    compressed: u64,
    decompressed: u64,
}

/// Run `body` over the tar entries of a layer.
///
/// Reader chain for gzip layers:
/// `file → HashingReader (compressed bytes + SHA256) → GzDecoder
///        → MeteringReader (decompressed bytes) → Archive`.
/// For plain tar layers the hashing reader is itself metered and its
/// decompressed counter is copied from the compressed byte count.
fn with_archive<T>(
    blob_path: &Path,
    gzip: bool,
    limits: &Limits,
    body: impl FnOnce(&mut dyn Read) -> Result<T>,
) -> Result<(T, StreamStats)> {
    let f = File::open(blob_path).map_err(Error::io)?;
    let buffered = BufReader::with_capacity(256 * 1024, f);

    if gzip {
        let mut h = HashingReader::new(buffered, Usage::default(), limits.clone());
        let mut m = MeteringReader::new(gzip_decoder(&mut h), Usage::default(), limits.clone());
        let value = body(&mut m)?;
        // Drain to physical EOF so the digest covers the whole blob even though
        // the tar parser stopped at the terminator blocks.
        let trailing = crate::io_util::drain_to_eof(&mut m).map_err(Error::io)?;
        let (du, diff_digest) = m.into_parts();
        let decompressed = du.decompressed + trailing;
        let (digest, cu) = h.finish();
        Ok((
            value,
            StreamStats {
                digest,
                diff_digest,
                compressed: cu.compressed,
                decompressed,
            },
        ))
    } else {
        // Plain tar: hash + compressed budget; drain trailing terminator
        // bytes through the hashing reader. Uncompressed digest == diff_id.
        let mut h = HashingReader::new(buffered, Usage::default(), limits.clone());
        let value = body(&mut h)?;
        crate::io_util::drain_to_eof(&mut h).map_err(Error::io)?;
        let (digest, cu) = h.finish();
        Ok((
            value,
            StreamStats {
                digest: digest.clone(),
                diff_digest: digest,
                compressed: cu.compressed,
                decompressed: cu.compressed,
            },
        ))
    }
}

// ───────────────────────── verify pass ─────────────────────────

/// Verify one layer: blob digest, uncompressed diff_id, limits, path safety,
/// and complete readability. `expected_diff` is the config `rootfs.diff_ids`
/// entry when present.
#[allow(clippy::too_many_arguments)]
pub fn scan_verify(
    index: usize,
    blob_path: &Path,
    expected_digest: &str,
    expected_diff: Option<&str>,
    gzip: bool,
    limits: &Limits,
) -> Result<LayerReport> {
    let mut report = LayerReport {
        index,
        ..LayerReport::default()
    };

    let ((), stats) = with_archive(blob_path, gzip, limits, |reader| {
        let mut archive = Archive::new(reader);
        // The archive itself is metered by the chain; payload accounting needs
        // its own usage object.
        let mut payload_usage = Usage::default();
        let entries = archive.entries().map_err(|e| Error::tar("entries", e))?;
        for entry_res in entries {
            let mut entry = entry_res.map_err(|e| map_tar_err(e, "verify"))?;
            payload_usage.count_entry(limits)?;
            inspect_entry(&mut entry, index, limits, &mut payload_usage)?;
            report.entries_seen += 1;
        }
        report.payload_bytes = payload_usage.payload;
        Ok(())
    })?;

    if stats.digest != expected_digest {
        return Err(Error::DigestMismatch {
            layer_index: index,
            expected: expected_digest.to_string(),
            actual: stats.digest,
        });
    }
    if let Some(want) = expected_diff {
        if stats.diff_digest != want {
            // diff_id mismatch is a digest failure of the uncompressed content.
            return Err(Error::DigestMismatch {
                layer_index: index,
                expected: want.to_string(),
                actual: stats.diff_digest,
            });
        }
    }
    report.digest = stats.digest;
    report.compressed_size = stats.compressed;
    report.decompressed_size = stats.decompressed;
    Ok(report)
}

/// Validate a single entry without writing anything.
fn inspect_entry<R: Read>(
    entry: &mut tar::Entry<R>,
    layer_index: usize,
    limits: &Limits,
    usage: &mut Usage,
) -> Result<()> {
    let raw_name = entry.path_bytes().into_owned();
    let etype = entry.header().entry_type();

    if matches!(
        etype,
        EntryType::XGlobalHeader
            | EntryType::XHeader
            | EntryType::GNULongName
            | EntryType::GNULongLink
    ) {
        // pax/gnu metadata consumed by the tar implementation; skip.
        return drain_entry(entry, entry.size(), usage, limits).map(|_| ());
    }

    if matches!(
        etype,
        EntryType::Block | EntryType::Char | EntryType::Fifo | EntryType::Continuous
    ) {
        // Continuous ("755 M") is treated as a device-like oddity and refused;
        // GNU sparse files are reported separately by the tar crate.
        return Err(Error::UnsupportedEntryType(
            String::from_utf8_lossy(&raw_name).into_owned(),
        ));
    }

    let rel = normalize_entry_name(&raw_name, limits)?;

    match etype {
        EntryType::Regular | EntryType::GNUSparse => {
            drain_entry(entry, entry.size(), usage, limits)?;
        }
        EntryType::Link => {
            usage.count_hard_link(limits)?;
            let lname = link_bytes(entry)?;
            let target = normalize_link_target(&lname, limits)?;
            // Hard links may never target directories.
            if target.file_name().is_none() {
                return Err(Error::Conflict(format!(
                    "hard link target invalid: {}",
                    target.as_str()
                )));
            }
        }
        EntryType::Symlink => {
            let lname = link_bytes(entry)?;
            validate_symlink_target(&rel, &lname, limits)?;
        }
        EntryType::Directory => {}
        other => {
            return Err(Error::UnsupportedEntryType(format!(
                "{} ({:?})",
                rel.as_str(),
                other
            )));
        }
    }
    let _ = layer_index;
    Ok(())
}

// ───────────────────────── apply pass ─────────────────────────

pub struct ApplyOutcome {
    pub report: LayerReport,
}

#[allow(clippy::too_many_arguments)]
pub fn scan_apply(
    index: usize,
    blob_path: &Path,
    expected_digest: &str,
    media_type: String,
    gzip: bool,
    limits: &Limits,
    rootfs: &Path,
    state: &mut RootfsState,
) -> Result<ApplyOutcome> {
    let digest_for_layers = expected_digest.to_string();
    let mut report = LayerReport {
        index,
        media_type,
        ..LayerReport::default()
    };

    let mut in_layer: BTreeSet<String> = BTreeSet::new();
    let mut pending_links: BTreeMap<String, (String, RelPath)> = BTreeMap::new();
    let mut payload_usage = Usage::default();

    let ((), stats) = with_archive(blob_path, gzip, limits, |reader| {
        let mut archive = Archive::new(reader);
        let entries = archive.entries().map_err(|e| Error::tar("entries", e))?;
        for entry_res in entries {
            let mut entry = entry_res.map_err(|e| map_tar_err(e, "apply"))?;
            payload_usage.count_entry(limits)?;
            report.entries_seen += 1;
            apply_entry(
                &mut entry,
                index,
                &digest_for_layers,
                limits,
                &mut payload_usage,
                rootfs,
                state,
                &mut in_layer,
                &mut pending_links,
                &mut report,
            )?;
        }
        Ok(())
    })?;

    if stats.digest != expected_digest {
        return Err(Error::DigestMismatch {
            layer_index: index,
            expected: expected_digest.to_string(),
            actual: stats.digest,
        });
    }

    // Resolve deferred hard links against the final state of this layer.
    for (link_name, (_order, target)) in pending_links.iter() {
        let link = normalize_entry_name(link_name.as_bytes(), limits)?;
        resolve_hard_link(rootfs, state, index, &digest_for_layers, &link, target)?;
        report.hardlinks += 1;
    }

    report.digest = stats.digest;
    report.compressed_size = stats.compressed;
    report.decompressed_size = stats.decompressed;
    report.payload_bytes = payload_usage.payload;
    Ok(ApplyOutcome { report })
}

#[allow(clippy::too_many_arguments)]
fn apply_entry<R: Read>(
    entry: &mut tar::Entry<R>,
    index: usize,
    digest: &str,
    limits: &Limits,
    usage: &mut Usage,
    rootfs: &Path,
    state: &mut RootfsState,
    in_layer: &mut BTreeSet<String>,
    pending_links: &mut BTreeMap<String, (String, RelPath)>,
    report: &mut LayerReport,
) -> Result<()> {
    let raw_name = entry.path_bytes().into_owned();
    let etype = entry.header().entry_type();

    if matches!(
        etype,
        EntryType::XGlobalHeader
            | EntryType::XHeader
            | EntryType::GNULongName
            | EntryType::GNULongLink
    ) {
        drain_entry(entry, entry.size(), usage, limits)?;
        return Ok(());
    }
    if matches!(
        etype,
        EntryType::Block | EntryType::Char | EntryType::Fifo | EntryType::Continuous
    ) {
        return Err(Error::UnsupportedEntryType(
            String::from_utf8_lossy(&raw_name).into_owned(),
        ));
    }

    let rel = normalize_entry_name(&raw_name, limits)?;
    let name = rel.as_str();

    // ── whiteout handling (depends only on file name of the leaf) ──
    let leaf_owned = rel.file_name().map(str::to_string);
    if let Some(leaf) = leaf_owned {
        if leaf == OPAQUE_WHITEOUT {
            return apply_opaque(rel, rootfs, state, in_layer, report);
        }
        if let Some(shadowed) = leaf.strip_prefix(WHITEOUT_PREFIX) {
            return apply_whiteout(
                rel,
                shadowed,
                rootfs,
                state,
                in_layer,
                pending_links,
                usage,
                limits,
                report,
            );
        }
    }

    // A real entry at this name supersedes any deferred hard link for it.
    pending_links.remove(&name);

    assert_no_symlink_ancestor(&rel, &state.symlinks)?;

    match etype {
        EntryType::Directory => {
            prepare_slot(rootfs, state, &rel, true)?;
            ensure_parent_dirs(rootfs, state, &rel, index, digest, in_layer)?;
            fsops::mkdir(&rel.under(rootfs), mode_of(entry))?;
            state
                .provenance
                .insert(name.clone(), (index, digest.to_string(), "dir"));
            state.symlinks.remove(&name);
            in_layer.insert(name);
            report.dirs += 1;
        }
        EntryType::Regular | EntryType::GNUSparse => {
            if entry.size() > limits.max_single_file_bytes {
                return Err(Error::limit(format!(
                    "file {name} declares {} bytes, limit {}",
                    entry.size(),
                    limits.max_single_file_bytes
                )));
            }
            prepare_slot(rootfs, state, &rel, false)?;
            ensure_parent_dirs(rootfs, state, &rel, index, digest, in_layer)?;
            let dst = rel.under(rootfs);
            let file_mode = mode_of(entry);
            let limited = LimitedWrite {
                inner: (&mut *entry) as &mut dyn Read,
                remaining: limits.max_single_file_bytes,
                written: 0,
                usage,
                limits,
            };
            let n = fsops::write_file_atomic(&dst, file_mode, limited)?;
            usage.add_payload(n, limits)?;
            state
                .provenance
                .insert(name.clone(), (index, digest.to_string(), "file"));
            state.symlinks.remove(&name);
            in_layer.insert(name);
            report.files += 1;
        }
        EntryType::Symlink => {
            let target_bytes = link_bytes(entry)?;
            let contained = validate_symlink_target(&rel, &target_bytes, limits)?;
            prepare_slot(rootfs, state, &rel, false)?;
            ensure_parent_dirs(rootfs, state, &rel, index, digest, in_layer)?;
            let target_str = std::str::from_utf8(&target_bytes).map_err(|_| Error::LinkEscape {
                link: name.clone(),
                target: String::from_utf8_lossy(&target_bytes).into_owned(),
            })?;
            fsops::symlink(target_str, &rel.under(rootfs))?;
            state
                .provenance
                .insert(name.clone(), (index, digest.to_string(), "symlink"));
            state.symlinks.insert(name.clone());
            in_layer.insert(name);
            report.symlinks += 1;
            let _ = contained;
        }
        EntryType::Link => {
            usage.count_hard_link(limits)?;
            let lname = link_bytes(entry)?;
            let target = normalize_link_target(&lname, limits)?;
            // Defer; record leaf order implicitly via map.
            pending_links.insert(
                name.clone(),
                (String::from_utf8_lossy(&raw_name).into_owned(), target),
            );
            in_layer.insert(name);
        }
        other => {
            return Err(Error::UnsupportedEntryType(format!(
                "{} ({:?})",
                name, other
            )));
        }
    }
    Ok(())
}

fn mode_of<R: Read>(entry: &tar::Entry<R>) -> u32 {
    entry.header().mode().unwrap_or(0o644)
}

/// Extract a mandatory link target (hard/symlink) as owned bytes. A header
/// missing the target is a corrupt archive.
fn link_bytes<R: Read>(entry: &tar::Entry<R>) -> Result<Vec<u8>> {
    match entry.link_name_bytes() {
        Some(Cow::Borrowed(b)) => Ok(b.to_vec()),
        Some(Cow::Owned(v)) => Ok(v),
        None => Err(Error::corrupt(format!(
            "entry {} has no link target",
            String::from_utf8_lossy(&entry.path_bytes())
        ))),
    }
}

/// Remove a subtree from provenance + disk. Deepest paths first on disk.
fn remove_subtree(
    rootfs: &Path,
    state: &mut RootfsState,
    prefix: &str,
    in_layer: &BTreeSet<String>,
    pending_links: &mut BTreeMap<String, (String, RelPath)>,
) {
    let victims: Vec<String> = state
        .provenance
        .range(prefix.to_string()..)
        .take_while(|(k, _)| *k == prefix || k.starts_with(&format!("{prefix}/")))
        .filter(|(k, _)| !in_layer.contains(*k))
        .map(|(k, _)| k.clone())
        .collect();
    for v in &victims {
        state.provenance.remove(v);
        state.symlinks.remove(v);
        pending_links.remove(v);
    }
    // Delete deepest first.
    for v in victims.iter().rev() {
        let rel = match rebuild_rel(v) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let _ = fsops::remove_any(&rel.under(rootfs));
    }
}

fn rebuild_rel(path: &str) -> Result<RelPath> {
    // Paths in the map are already normalized; rebuild without limits risk.
    let components: Vec<String> = path.split('/').map(String::from).collect();
    Ok(RelPath::from_components(components))
}

fn apply_opaque(
    marker: RelPath,
    rootfs: &Path,
    state: &mut RootfsState,
    in_layer: &BTreeSet<String>,
    report: &mut LayerReport,
) -> Result<()> {
    // Marker is `<dir>/.wh..wh..opq`; the directory is everything but the leaf.
    let parent: Vec<String> = marker.parent_components().to_vec();
    let prefix = parent.join("/");
    // A top-level opaque marker (opaque "/") clears all lower content.
    let key = if prefix.is_empty() {
        String::new()
    } else {
        prefix.clone()
    };
    if key.is_empty() {
        let all: Vec<String> = state
            .provenance
            .keys()
            .filter(|k| !in_layer.contains(*k))
            .cloned()
            .collect();
        for v in all.iter().rev() {
            state.provenance.remove(v);
            state.symlinks.remove(v);
            if let Ok(rel) = rebuild_rel(v) {
                let _ = fsops::remove_any(&rel.under(rootfs));
            }
        }
    } else {
        remove_subtree(rootfs, state, &key, in_layer, &mut BTreeMap::new());
    }
    report.opaque_dirs += 1;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn apply_whiteout(
    marker: RelPath,
    shadowed_leaf: &str,
    rootfs: &Path,
    state: &mut RootfsState,
    in_layer: &BTreeSet<String>,
    pending_links: &mut BTreeMap<String, (String, RelPath)>,
    usage: &mut Usage,
    limits: &Limits,
    report: &mut LayerReport,
) -> Result<()> {
    let _ = (usage, limits);
    let mut comps: Vec<String> = marker.parent_components().to_vec();
    comps.push(shadowed_leaf.to_string());
    let victim = RelPath::from_components(comps);
    let vname = victim.as_str();

    // Whiteouts never remove content already produced by THIS layer.
    if in_layer.contains(&vname) {
        report.whiteouts += 1;
        return Ok(());
    }
    remove_subtree(rootfs, state, &vname, in_layer, pending_links);
    report.whiteouts += 1;
    Ok(())
}

/// Make room for an incoming entry at `rel`: if the existing inode type
/// conflicts, remove it (and its subtree for directories).
fn prepare_slot(
    rootfs: &Path,
    state: &mut RootfsState,
    rel: &RelPath,
    incoming_is_dir: bool,
) -> Result<()> {
    let name = rel.as_str();
    let existing = state.provenance.get(&name).map(|(_, _, k)| *k);
    match existing {
        Some("dir") if !incoming_is_dir => {
            // Directory replaced by file/symlink: clear its lower subtree.
            let empty = BTreeSet::new();
            let mut no_pending = BTreeMap::new();
            remove_subtree(rootfs, state, &name, &empty, &mut no_pending);
        }
        Some(kind) if kind != "dir" && incoming_is_dir => {
            let p = rel.under(rootfs);
            fsops::remove_any(&p)?;
            state.provenance.remove(&name);
            state.symlinks.remove(&name);
        }
        _ => {}
    }
    Ok(())
}

fn ensure_parent_dirs(
    rootfs: &Path,
    state: &mut RootfsState,
    rel: &RelPath,
    index: usize,
    digest: &str,
    in_layer: &mut BTreeSet<String>,
) -> Result<()> {
    let comps = rel.components();
    let mut acc = String::new();
    for c in &comps[..comps.len().saturating_sub(1)] {
        if !acc.is_empty() {
            acc.push('/');
        }
        acc.push_str(c);
        if !state.provenance.contains_key(&acc) {
            let parent_rel = rebuild_rel(&acc)?;
            fsops::mkdir(&parent_rel.under(rootfs), 0o755)?;
            state
                .provenance
                .insert(acc.clone(), (index, digest.to_string(), "dir"));
            in_layer.insert(acc.clone());
        }
    }
    Ok(())
}

fn resolve_hard_link(
    rootfs: &Path,
    state: &mut RootfsState,
    index: usize,
    digest: &str,
    link: &RelPath,
    target: &RelPath,
) -> Result<()> {
    let tname = target.as_str();
    let (target_layer, target_digest, kind) = state
        .provenance
        .get(&tname)
        .ok_or_else(|| Error::Conflict(format!("hard link target missing: {tname}")))?
        .clone();
    if kind == "dir" {
        return Err(Error::Conflict(format!(
            "hard link to directory rejected: {tname}"
        )));
    }
    assert_no_symlink_ancestor(link, &state.symlinks)?;
    let target_path = target.under(rootfs);
    let link_path = link.under(rootfs);
    fsops::hardlink(&target_path, &link_path)?;
    // A hard link aliases the same inode: it inherits the target content's
    // provenance, but the name entered the namespace in this layer.
    let final_kind = if kind == "symlink" { "symlink" } else { "file" };
    state
        .provenance
        .insert(link.as_str(), (target_layer, target_digest, final_kind));
    let _ = (index, digest);
    Ok(())
}

// ───────────────────── small adapters / helpers ─────────────────────

/// Reader adapter enforcing the per-file + payload budgets while writing.
struct LimitedWrite<'a> {
    inner: &'a mut dyn Read,
    remaining: u64,
    written: u64,
    usage: &'a mut Usage,
    limits: &'a Limits,
}

impl<'a> Read for LimitedWrite<'a> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let max = buf.len().min(self.remaining as usize);
        let n = self.inner.read(&mut buf[..max])?;
        if n > 0 {
            self.remaining -= n as u64;
            self.written += n as u64;
            self.usage
                .add_payload(n as u64, self.limits)
                .map_err(std::io::Error::other)?;
        }
        Ok(n)
    }
}

fn map_tar_err(e: std::io::Error, phase: &str) -> Error {
    // Decompression failures arrive wrapped from the gzip layer.
    let msg = e.to_string();
    let lower = e.get_ref().map(|c| c.to_string()).unwrap_or_default();
    if msg.contains("gzip") || lower.contains("gzip") || msg.contains("deflate") {
        Error::Gzip {
            context: format!("layer {phase}"),
            source: msg,
        }
    } else {
        match crate::io_util::decode_io_err(e) {
            Error::Io(m) if m.contains("gzip") => Error::Gzip {
                context: format!("layer {phase}"),
                source: m,
            },
            Error::Io(m) => Error::Tar {
                context: format!("layer {phase}"),
                source: m,
            },
            other => other,
        }
    }
}
