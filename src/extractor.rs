//! Layered whitelist extraction.
//!
//! Every layer is applied in digest order in two passes:
//!
//! 1. **Whiteout pass** — stream the whole archive and collect opaque
//!    directories (`.wh..wh..opq`) and deleted basenames (`.wh.<name>`),
//!    then remove the affected files / directory subtrees in the staging
//!    root. Whiteout marker entries never appear on disk.
//! 2. **Content pass** — stream the archive again and materialise only the
//!    whitelisted tar entry types: regular files (incl. hardlinks),
//!    directories and safe symlinks. Device nodes, FIFOs, sockets, GNU
//!    sparse files and unknown extension entries are rejected.
//!
//! "Delete then recreate in the same layer" works: pass 1 removes the old
//! entry, pass 2 creates the new one. An opaque marker in `mydir/` discards
//! all pre-existing children of `mydir` before the layer's own contents
//! land.

use std::collections::HashMap;
use std::fs;
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;
use tar::{Archive, EntryType};

use crate::digest::OciDigest;
use crate::error::{AppError, AppResult};
use crate::limits::Limits;
use crate::oci::{LayerCompression, LayerRef};
use crate::pathsafe::{check_hardlink_target, check_symlink_target, sanitize_relative};

const WHITEOUT_PREFIX: &str = ".wh.";
const OPAQUE_WHITEOUT: &str = ".wh..wh..opq";

/// What an extracted node is. Only `File` contributes to the file-count
/// quota (hardlinks included — each is its own directory entry).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    File,
    Directory,
    Symlink,
}

#[derive(Debug, Clone)]
pub struct NodeInfo {
    /// Digest of the layer that last materialised this path.
    pub layer: OciDigest,
    /// Position of that layer in the image (0-based).
    pub layer_index: usize,
    pub kind: NodeKind,
}

/// First-write inode target for hardlink resolution within a layer.
struct FirstWrite {
    path: PathBuf,
}

/// Per-rebuild extraction state, living across all layers.
pub struct Extractor<'a> {
    root: PathBuf,
    limits: &'a Limits,
    /// Decompressed bytes charged across all layers so far.
    total_uncompressed: u64,
    /// Number of file paths currently materialised (incl. hardlinks).
    file_count: u64,
    pub provenance: HashMap<PathBuf, NodeInfo>,
}

// --------------------------------------------------------------------------
// Layer streams: gzip/plain, with byte counting or digest hashing.
// --------------------------------------------------------------------------

enum RawStream {
    Plain(io::BufReader<fs::File>),
    Gzip(GzDecoder<io::BufReader<fs::File>>),
}

impl Read for RawStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            RawStream::Plain(r) => r.read(buf),
            RawStream::Gzip(r) => r.read(buf),
        }
    }
}

/// Decompressed-byte counting wrapper (for the two apply passes).
struct CountingStream {
    inner: RawStream,
    count: u64,
    limit: u64,
}

impl CountingStream {
    fn open(layer: &LayerRef, limit: u64) -> AppResult<Self> {
        let f = io::BufReader::new(fs::File::open(&layer.blob)?);
        let inner = match layer.compression {
            LayerCompression::Plain => RawStream::Plain(f),
            LayerCompression::Gzip => RawStream::Gzip(GzDecoder::new(f)),
        };
        Ok(Self {
            inner,
            count: 0,
            limit,
        })
    }

    fn bytes_read(&self) -> u64 {
        self.count
    }
}

impl Read for CountingStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.count = self.count.saturating_add(n as u64);
        if self.count > self.limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                crate::limits::LimitExceeded(format!(
                    "decompressed layer exceeds {}-byte limit",
                    self.limit
                )),
            ));
        }
        Ok(n)
    }
}

// --------------------------------------------------------------------------
// Extractor
// --------------------------------------------------------------------------

impl<'a> Extractor<'a> {
    pub fn new(root: PathBuf, limits: &'a Limits) -> Self {
        Self {
            root,
            limits,
            total_uncompressed: 0,
            file_count: 0,
            provenance: HashMap::new(),
        }
    }

    /// Verify one layer blob.
    ///
    /// Phase A: SHA-256 the *stored* (possibly compressed) bytes — OCI layer
    /// digests cover exactly those bytes.
    /// Phase B: only if the digest matched, fully decompress (size-capped),
    /// which validates the gzip CRC/length trailer and surfaces truncation.
    ///
    /// Nothing is written to the root here.
    pub fn verify_layer(&self, layer: &LayerRef) -> AppResult<()> {
        let stored = fs::read(&layer.blob)?;
        if (stored.len() as u64) > self.limits.max_verify_bytes {
            return Err(AppError::limit(format!(
                "stored layer blob {} exceeds {}-byte verification limit",
                layer.digest, self.limits.max_verify_bytes
            )));
        }
        let actual = OciDigest::of_bytes(&stored);
        if !layer.digest.verify(&actual) {
            return Err(AppError::DigestMismatch {
                descriptor: format!("layer {}", layer.digest),
                expected: layer.digest.to_string(),
                actual: actual.to_string(),
            });
        }

        // Digest proven — now validate that the blob is a well-formed archive
        // stream before any of it is applied.
        match layer.compression {
            LayerCompression::Plain => {
                // Tar structure is parsed streaming during the apply passes;
                // here we only needed the stored-byte digest.
            }
            LayerCompression::Gzip => {
                let mut decoder = GzDecoder::new(stored.as_slice());
                let mut sink =
                    LimitWriter::new(self.limits.max_layer_uncompressed, "decompressed layer");
                io::copy(&mut decoder, &mut sink).map_err(|e| map_archive_io(e, "gzip"))?;
            }
        }
        tracing::debug!(%layer.digest, stored_bytes = stored.len(), "layer digest verified");
        Ok(())
    }

    /// Apply one already-verified layer to the staging root.
    pub fn apply_layer(&mut self, layer: &LayerRef, layer_index: usize) -> AppResult<()> {
        // ---- pass 1: collect + apply whiteouts ---------------------------
        let mut opaque_dirs: Vec<PathBuf> = Vec::new();
        let mut whiteouts: Vec<PathBuf> = Vec::new();
        let decompressed = self.scan_whiteouts(layer, &mut opaque_dirs, &mut whiteouts)?;
        self.charge_uncompressed(decompressed)?;

        for dir in &opaque_dirs {
            self.apply_opaque(dir)?;
        }
        for victim in &whiteouts {
            self.apply_whiteout(victim)?;
        }

        // ---- pass 2: materialise whitelisted contents --------------------
        self.write_contents(layer, layer_index)?;
        Ok(())
    }

    /// Pass 1 — stream the archive once, collecting whiteout markers.
    fn scan_whiteouts(
        &self,
        layer: &LayerRef,
        opaque_dirs: &mut Vec<PathBuf>,
        whiteouts: &mut Vec<PathBuf>,
    ) -> AppResult<u64> {
        let stream = CountingStream::open(layer, self.limits.max_layer_uncompressed)?;
        let mut ar = Archive::new(stream);
        let entries = ar.entries().map_err(|e| map_archive_io(e, "tar"))?;

        let mut count = 0u64;
        for res in entries {
            let mut entry = res.map_err(|e| map_archive_io(e, "tar entry"))?;
            count += 1;
            if count > self.limits.max_entries_per_layer {
                return Err(AppError::limit(format!(
                    "layer {}: more than {} entries",
                    layer.digest, self.limits.max_entries_per_layer
                )));
            }
            let raw = entry.path().map_err(|e| map_archive_io(e, "tar path"))?;
            let rel = sanitize_relative(&raw, self.limits)?;
            if let Some(name) = rel.file_name().and_then(|n| n.to_str()) {
                if name == OPAQUE_WHITEOUT {
                    let dir = rel
                        .parent()
                        .map(|p| p.to_path_buf())
                        .filter(|p| !p.as_os_str().is_empty())
                        .unwrap_or_else(|| PathBuf::from("."));
                    opaque_dirs.push(dir);
                } else if let Some(base) = name.strip_prefix(WHITEOUT_PREFIX) {
                    if !base.is_empty() {
                        whiteouts.push(rel.with_file_name(base));
                    }
                }
            }
            // Drain payload (markers have none, but every entry must be
            // consumed for the stream to advance).
            io::copy(&mut entry, &mut io::sink())
                .map_err(|e| map_archive_io(e, "whiteout scan"))?;
        }
        let stream = ar.into_inner();
        Ok(stream.bytes_read())
    }

    fn apply_opaque(&mut self, dir: &Path) -> AppResult<()> {
        if dir == Path::new(".") {
            return Err(AppError::Whiteout(
                "opaque whiteout at archive root is not allowed".to_string(),
            ));
        }
        let abs = self.root.join(dir);
        let meta = match abs.symlink_metadata() {
            Ok(m) => m,
            Err(_) => return Ok(()),
        };
        if meta.is_dir() {
            let children: Vec<PathBuf> = fs::read_dir(&abs)?
                .map(|e| e.map(|e| e.path()))
                .collect::<io::Result<_>>()?;
            for child in children {
                if fs::symlink_metadata(&child)?.is_dir() {
                    fs::remove_dir_all(&child)?;
                } else {
                    fs::remove_file(&child)?;
                }
            }
        } else {
            // The layer declares an opaque *directory* at this path, but a
            // previous layer left a symlink or file here. Never follow it:
            // unlink the node so pass 2 materialises a real directory.
            fs::remove_file(&abs)?;
            self.forget_subtree(dir);
        }
        self.forget_descendants(dir);
        Ok(())
    }

    fn apply_whiteout(&mut self, victim: &Path) -> AppResult<()> {
        let abs = self.root.join(victim);
        match abs.symlink_metadata() {
            Ok(meta) => {
                if meta.is_dir() {
                    fs::remove_dir_all(&abs)?;
                } else {
                    fs::remove_file(&abs)?;
                }
            }
            Err(_) => return Ok(()), // absent — whiteout is a no-op
        }
        self.forget_subtree(victim);
        Ok(())
    }

    /// Forget `prefix` itself and everything below it, adjusting file quota.
    fn forget_subtree(&mut self, prefix: &Path) {
        let doomed: Vec<PathBuf> = self
            .provenance
            .keys()
            .filter(|k| k.starts_with(prefix))
            .cloned()
            .collect();
        for k in doomed {
            if let Some(info) = self.provenance.remove(&k) {
                if info.kind == NodeKind::File {
                    self.file_count = self.file_count.saturating_sub(1);
                }
            }
        }
    }

    /// Forget strict descendants of `dir` (the opaque dir itself survives).
    fn forget_descendants(&mut self, dir: &Path) {
        let doomed: Vec<PathBuf> = self
            .provenance
            .keys()
            .filter(|k| k.starts_with(dir) && *k != dir)
            .cloned()
            .collect();
        for k in doomed {
            if let Some(info) = self.provenance.remove(&k) {
                if info.kind == NodeKind::File {
                    self.file_count = self.file_count.saturating_sub(1);
                }
            }
        }
    }

    /// Pass 2 — materialise the archive's whitelisted contents.
    fn write_contents(&mut self, layer: &LayerRef, layer_index: usize) -> AppResult<()> {
        let stream = CountingStream::open(layer, self.limits.max_layer_uncompressed)?;
        let mut ar = Archive::new(stream);
        let entries = ar.entries().map_err(|e| map_archive_io(e, "tar"))?;

        let mut first_writes: HashMap<PathBuf, FirstWrite> = HashMap::new();
        let mut count = 0u64;

        for res in entries {
            let mut entry = res.map_err(|e| map_archive_io(e, "tar entry"))?;
            count += 1;
            if count > self.limits.max_entries_per_layer {
                return Err(AppError::limit(format!(
                    "layer {}: more than {} entries",
                    layer.digest, self.limits.max_entries_per_layer
                )));
            }

            let raw = entry.path().map_err(|e| map_archive_io(e, "tar path"))?;
            let rel = sanitize_relative(&raw, self.limits)?;

            // Whiteout markers never materialise.
            if let Some(name) = rel.file_name().and_then(|n| n.to_str()) {
                if name == OPAQUE_WHITEOUT || name.starts_with(WHITEOUT_PREFIX) {
                    io::copy(&mut entry, &mut io::sink())
                        .map_err(|e| map_archive_io(e, "whiteout drain"))?;
                    continue;
                }
            }

            let mode = entry.header().mode().unwrap_or(0);
            match entry.header().entry_type() {
                EntryType::Directory => {
                    self.make_dir(&rel, mode, layer, layer_index)?;
                    self.record(&rel, layer, layer_index, NodeKind::Directory);
                }
                EntryType::Regular | EntryType::Continuous => {
                    // A PAX `GNU.sparse.*` extension on a regular-looking
                    // entry means a sparse file; sparse materialisation is
                    // outside the whitelist, so reject it.
                    if let Some(pax) = entry
                        .pax_extensions()
                        .map_err(|e| map_archive_io(e, "pax"))?
                    {
                        for ext in pax {
                            let ext = ext.map_err(|e| map_archive_io(e, "pax ext"))?;
                            if ext.key_bytes().starts_with(b"GNU.sparse.") {
                                return Err(AppError::rejected(
                                    rel.display().to_string(),
                                    "pax sparse files are not whitelisted",
                                ));
                            }
                        }
                    }
                    let declared = entry.header().size().unwrap_or(0);
                    if declared > self.limits.max_file_size {
                        return Err(AppError::limit(format!(
                            "file {} declares {} bytes (max {})",
                            rel.display(),
                            declared,
                            self.limits.max_file_size
                        )));
                    }
                    let abs = self.root.join(&rel);
                    self.make_leaf_parent(&rel, layer, layer_index)?;
                    self.replace_leaf(&abs, &rel)?;

                    let mut file = fs::File::create(&abs)?;
                    // Hard cap actual bytes even if the header lied.
                    let mut limited =
                        (&mut entry).take(self.limits.max_file_size.saturating_add(1));
                    let n = io::copy(&mut limited, &mut file)
                        .map_err(|e| map_archive_io(e, "file write"))?;
                    if n > self.limits.max_file_size {
                        return Err(AppError::limit(format!(
                            "file {} larger than {} bytes",
                            rel.display(),
                            self.limits.max_file_size
                        )));
                    }
                    file.sync_all().ok();
                    drop(file);

                    set_safe_mode(&abs, mode, false)?;
                    first_writes.insert(rel.clone(), FirstWrite { path: abs.clone() });
                    self.record(&rel, layer, layer_index, NodeKind::File);
                }
                EntryType::Symlink => {
                    let target = read_link_name(&entry, &rel)?;
                    if target.len() > self.limits.max_path_len {
                        return Err(AppError::limit("symlink target too long"));
                    }
                    // Lexical confinement check (see pathsafe.rs).
                    let _confined = check_symlink_target(&rel, &target, self.limits)?;
                    let abs = self.root.join(&rel);
                    self.make_leaf_parent(&rel, layer, layer_index)?;
                    self.replace_leaf(&abs, &rel)?;
                    let target_os = std::ffi::OsStr::from_bytes(&target);
                    std::os::unix::fs::symlink(target_os, &abs)?;
                    self.record(&rel, layer, layer_index, NodeKind::Symlink);
                }
                EntryType::Link => {
                    let target_raw = entry
                        .link_name()
                        .map_err(|e| map_archive_io(e, "hardlink name"))?
                        .ok_or_else(|| {
                            AppError::rejected(rel.display().to_string(), "hardlink without target")
                        })?;
                    let target_rel = check_hardlink_target(&target_raw, &rel, self.limits)?;
                    let target_abs = self
                        .resolve_first_write(&target_rel, &first_writes)
                        .ok_or_else(|| {
                            AppError::rejected(
                                rel.display().to_string(),
                                format!(
                                    "hardlink target is not a materialised regular file: {}",
                                    target_rel.display()
                                ),
                            )
                        })?;
                    let abs = self.root.join(&rel);
                    self.make_leaf_parent(&rel, layer, layer_index)?;
                    self.replace_leaf(&abs, &rel)?;
                    fs::hard_link(&target_abs, &abs)?;
                    self.record(&rel, layer, layer_index, NodeKind::File);
                }
                EntryType::Char | EntryType::Block | EntryType::Fifo | EntryType::GNUSparse => {
                    return Err(AppError::rejected(
                        rel.display().to_string(),
                        format!(
                            "disallowed tar entry type: {}",
                            entry_type_name(entry.header().entry_type())
                        ),
                    ));
                }
                _ => {
                    // GNULongName / GNULongLink / xheaders are consumed by
                    // tar-rs and not yielded; anything else is outside the
                    // whitelist and is rejected.
                    return Err(AppError::rejected(
                        rel.display().to_string(),
                        format!(
                            "unknown/unsupported tar entry type: {:?}",
                            entry.header().entry_type()
                        ),
                    ));
                }
            }
        }
        Ok(())
    }

    fn charge_uncompressed(&mut self, bytes: u64) -> AppResult<()> {
        self.total_uncompressed = self.total_uncompressed.saturating_add(bytes);
        if self.total_uncompressed > self.limits.max_total_uncompressed {
            return Err(AppError::limit(format!(
                "all layers: {} decompressed bytes exceed {}-byte image limit",
                self.total_uncompressed, self.limits.max_total_uncompressed
            )));
        }
        Ok(())
    }

    /// Materialise `rel` as a directory, creating any missing parents as
    /// real directories. Never follows symlinks: an existing symlink/file
    /// in the way is removed first. Newly created directories (including
    /// implicit parents) are recorded in provenance for this layer.
    fn make_dir(
        &mut self,
        rel: &Path,
        mode: u32,
        layer: &LayerRef,
        layer_index: usize,
    ) -> AppResult<()> {
        let abs = self.root.join(rel);
        let mut created = false;
        match abs.symlink_metadata() {
            Ok(m) if m.is_dir() => {}
            Ok(_) => {
                fs::remove_file(&abs)?;
                self.forget_subtree(rel);
                fs::create_dir(&abs)?;
                created = true;
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                if let Some(parent) = rel.parent() {
                    if !parent.as_os_str().is_empty() {
                        self.make_dir(parent, 0o755, layer, layer_index)?;
                    }
                }
                fs::create_dir(&abs)?;
                created = true;
            }
            Err(e) => return Err(e.into()),
        }
        set_safe_mode(&abs, mode, true)?;
        if created {
            self.record(rel, layer, layer_index, NodeKind::Directory);
        }
        Ok(())
    }

    fn make_leaf_parent(
        &mut self,
        rel: &Path,
        layer: &LayerRef,
        layer_index: usize,
    ) -> AppResult<()> {
        if let Some(parent) = rel.parent() {
            if !parent.as_os_str().is_empty() {
                self.make_dir(parent, 0o755, layer, layer_index)?;
            }
        }
        Ok(())
    }

    /// Clear the leaf path so a fresh entry can land. Non-empty directories
    /// are never replaced silently.
    fn replace_leaf(&self, abs: &Path, rel: &Path) -> AppResult<()> {
        match abs.symlink_metadata() {
            Ok(m) if m.is_dir() => {
                let mut rd = fs::read_dir(abs)?;
                if rd.next().is_some() {
                    return Err(AppError::rejected(
                        rel.display().to_string(),
                        "refusing to overwrite a non-empty directory with a file/link",
                    ));
                }
                fs::remove_dir(abs)?;
            }
            Ok(_) => fs::remove_file(abs)?,
            Err(_) => {}
        }
        Ok(())
    }

    fn resolve_first_write(
        &self,
        target_rel: &Path,
        local: &HashMap<PathBuf, FirstWrite>,
    ) -> Option<PathBuf> {
        if let Some(w) = local.get(target_rel) {
            return Some(w.path.clone());
        }
        let abs = self.root.join(target_rel);
        match abs.symlink_metadata() {
            Ok(m) if m.is_file() => Some(abs),
            _ => None,
        }
    }

    /// Record provenance, maintaining the file-count quota against the
    /// previous kind at this path.
    fn record(&mut self, rel: &Path, layer: &LayerRef, idx: usize, kind: NodeKind) {
        let old = self.provenance.insert(
            rel.to_path_buf(),
            NodeInfo {
                layer: layer.digest.clone(),
                layer_index: idx,
                kind,
            },
        );
        match (old.map(|i| i.kind), kind) {
            (Some(NodeKind::File), NodeKind::File) | (None, NodeKind::Directory) => {}
            (_, NodeKind::File) => {
                self.file_count = self.file_count.saturating_add(1);
                if self.file_count > self.limits.max_file_count {
                    tracing::warn!(
                        "file count {} exceeds quota {}",
                        self.file_count,
                        self.limits.max_file_count
                    );
                }
            }
            (Some(NodeKind::File), _) => {
                self.file_count = self.file_count.saturating_sub(1);
            }
            _ => {}
        }
        if self.file_count > self.limits.max_file_count {
            // Enforced on the next fallible operation boundary; the direct
            // check is performed by the caller after each batch.
        }
    }

    pub fn check_file_quota(&self) -> AppResult<()> {
        if self.file_count > self.limits.max_file_count {
            return Err(AppError::limit(format!(
                "image contains more than {} files",
                self.limits.max_file_count
            )));
        }
        Ok(())
    }
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

fn entry_type_name(t: EntryType) -> &'static str {
    match t {
        EntryType::Char => "character device",
        EntryType::Block => "block device",
        EntryType::Fifo => "fifo",
        EntryType::GNUSparse => "gnu sparse file",
        _ => "unsupported",
    }
}

fn read_link_name<R: Read>(entry: &tar::Entry<'_, R>, rel: &Path) -> AppResult<Vec<u8>> {
    let cow = entry
        .link_name()
        .map_err(|e| map_archive_io(e, "link name"))?
        .ok_or_else(|| AppError::rejected(rel.display().to_string(), "link without target"))?;
    Ok(cow.as_os_str().as_bytes().to_vec())
}

fn set_safe_mode(path: &Path, mode: u32, is_dir: bool) -> AppResult<()> {
    use std::os::unix::fs::PermissionsExt;
    // Strip setuid/setgid; keep the sticky bit if explicitly present.
    let safe = if mode == 0 {
        if is_dir {
            0o755
        } else {
            0o644
        }
    } else {
        mode & 0o1777
    };
    fs::set_permissions(path, fs::Permissions::from_mode(safe))?;
    Ok(())
}

fn map_archive_io(e: io::Error, what: &str) -> AppError {
    // A streaming resource limit always wins over the generic
    // corrupt-archive classification for InvalidData errors.
    if let Some(limit) = e
        .get_ref()
        .and_then(|c| c.downcast_ref::<crate::limits::LimitExceeded>())
    {
        return AppError::limit(limit.0.clone());
    }
    let msg = e.to_string();
    if matches!(
        e.kind(),
        io::ErrorKind::InvalidData | io::ErrorKind::InvalidInput | io::ErrorKind::UnexpectedEof
    ) || msg.contains("gzip")
        || msg.contains("checksum")
        || msg.contains("corrupt")
        || msg.contains("unexpected EOF")
    {
        AppError::corrupt(format!("{what}: {msg}"))
    } else {
        AppError::Io(e)
    }
}

/// io::Write sink that errors once `max` bytes have been written.
struct LimitWriter {
    left: u64,
    what: String,
}

impl LimitWriter {
    fn new(max: u64, what: &str) -> Self {
        Self {
            left: max,
            what: what.to_string(),
        }
    }
}

impl io::Write for LimitWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = buf.len() as u64;
        if n > self.left {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                crate::limits::LimitExceeded(format!("limit exceeded while reading {}", self.what)),
            ));
        }
        self.left -= n;
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
