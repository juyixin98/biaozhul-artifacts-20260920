//! Build local OCI image fixtures used as example inputs and in the test
//! suite. Produces a standards-shaped OCI image-layout directory:
//!
//! ```text
//! <dir>/
//!   oci-layout
//!   index.json
//!   blobs/sha256/<digest>
//! ```
//!
//! The same builder can deliberately emit malicious layers (path traversal,
//! escaping symlinks, devices, corrupt gzip, wrong digests) so the unpacker's
//! rejection paths can be exercised against real bytes.

use std::fs;
use std::io::Write;
use std::path::Path;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub mod evil {
    //!! Entry constructors for malicious/edge layers.
    use super::*;

    pub fn traversal_escape() -> RawEntry {
        RawEntry {
            // Verbatim hostile member; normal set_path would also accept this
            // but we inject raw bytes to be unambiguous about intent.
            path: "../../etc/passwd_host".into(),
            kind: RawKind::RawTar(raw_tar_member(
                "../../etc/passwd_host",
                tar::EntryType::Regular,
                b"pwned",
                None,
            )),
        }
    }

    pub fn absolute_path() -> RawEntry {
        RawEntry {
            path: "/tmp/abs_escape".into(),
            kind: RawKind::RawTar(raw_tar_member(
                "/tmp/abs_escape",
                tar::EntryType::Regular,
                b"pwned",
                None,
            )),
        }
    }

    pub fn symlink_escape() -> RawEntry {
        RawEntry {
            path: "evil".into(),
            kind: RawKind::Symlink {
                target: "../../../host".into(),
            },
        }
    }

    pub fn symlink_dotdot_deep() -> RawEntry {
        // Link that only escapes after joining its own parent directory.
        RawEntry {
            path: "a/b/link".into(),
            kind: RawKind::Symlink {
                target: "../../../../escape".into(),
            },
        }
    }

    pub fn device() -> RawEntry {
        RawEntry {
            path: "dev/nullfake".into(),
            kind: RawKind::Char { major: 1, minor: 3 },
        }
    }

    /// A relative symlink that stays lexicaly inside root but is placed such
    /// that a *later* layer writing through it would escape if ancestor
    /// symlinks were followed.
    pub fn benign_symlink_dir() -> RawEntry {
        RawEntry {
            path: "linkdir".into(),
            kind: RawKind::Symlink {
                target: "real".into(),
            },
        }
    }
}

#[derive(Debug, Clone)]
pub enum RawKind {
    Dir,
    File {
        mode: u32,
        contents: Vec<u8>,
    },
    Symlink {
        target: String,
    },
    Hardlink {
        target: String,
    },
    Char {
        major: u32,
        minor: u32,
    },
    Whiteout {
        leaf: String,
    },
    OpaqueDir,
    /// Raw bytes appended verbatim into the tar stream (for hostile names the
    /// high-level builder would reject — traversal, absolute paths).
    RawTar(Vec<u8>),
}

#[derive(Debug, Clone)]
pub struct RawEntry {
    pub path: String,
    pub kind: RawKind,
}

impl RawEntry {
    pub fn dir_only(path: impl Into<String>) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::Dir,
        }
    }

    pub fn file(path: impl Into<String>, contents: impl Into<Vec<u8>>) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::File {
                mode: 0o644,
                contents: contents.into(),
            },
        }
    }

    pub fn file_mode(path: impl Into<String>, mode: u32, contents: impl Into<Vec<u8>>) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::File {
                mode,
                contents: contents.into(),
            },
        }
    }

    pub fn symlink(path: impl Into<String>, target: impl Into<String>) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::Symlink {
                target: target.into(),
            },
        }
    }

    pub fn hardlink(path: impl Into<String>, target: impl Into<String>) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::Hardlink {
                target: target.into(),
            },
        }
    }

    pub fn dir(path: impl Into<String>) -> Self {
        let p = path.into();
        RawEntry {
            path: p.clone(),
            kind: RawKind::Dir,
        }
    }

    pub fn whiteout(dir: impl Into<String>, leaf: impl Into<String>) -> Self {
        let dir = dir.into();
        let leaf = leaf.into();
        let path = if dir.is_empty() {
            format!(".wh.{leaf}")
        } else {
            format!("{}/.wh.{leaf}", dir.trim_end_matches('/'))
        };
        RawEntry {
            path,
            kind: RawKind::Whiteout { leaf },
        }
    }

    pub fn opaque(dir: impl Into<String>) -> Self {
        let dir = dir.into();
        RawEntry {
            path: if dir.is_empty() || dir == "." {
                ".wh..wh..opq".into()
            } else {
                format!("{}/.wh..wh..opq", dir.trim_end_matches('/'))
            },
            kind: RawKind::OpaqueDir,
        }
    }

    pub fn char_device(path: impl Into<String>, major: u32, minor: u32) -> Self {
        RawEntry {
            path: path.into(),
            kind: RawKind::Char { major, minor },
        }
    }
}

/// How a layer blob is stored/compressed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Compression {
    PlainTar,
    Gzip,
    /// Valid gzip stream whose *inflated* content is not a tar at all.
    GarbageGzip,
    /// Valid gzip of the tar with trailing bytes chopped off (truncated).
    GzipTruncated,
}

#[derive(Debug, Clone)]
pub struct LayerSpec {
    pub entries: Vec<RawEntry>,
    pub compression: Compression,
    /// Force the manifest descriptor digest to this value (tampering test).
    pub force_descriptor_digest: Option<String>,
    /// Force the config `rootfs.diff_ids[i]` to a wrong value while keeping the
    /// gzip blob and manifest digest valid (uncompressed verification test).
    pub force_diff_id: Option<String>,
}

impl LayerSpec {
    pub fn gzip(entries: Vec<RawEntry>) -> Self {
        LayerSpec {
            entries,
            compression: Compression::Gzip,
            force_descriptor_digest: None,
            force_diff_id: None,
        }
    }
    pub fn tar(entries: Vec<RawEntry>) -> Self {
        LayerSpec {
            entries,
            compression: Compression::PlainTar,
            force_descriptor_digest: None,
            force_diff_id: None,
        }
    }
}

/// Build a complete OCI fixture. Returns the layer digests actually written.
pub fn build_image(dir: impl AsRef<Path>, layers: &[LayerSpec]) -> anyhow::Result<BuiltImage> {
    let dir = dir.as_ref();
    let blob_dir = dir.join("blobs").join("sha256");
    fs::create_dir_all(&blob_dir)?;

    let mut layer_descriptors = Vec::new();
    let mut diff_ids = Vec::new();

    for layer in layers {
        let tar_bytes = build_tar(&layer.entries)?;
        let uncompressed_digest = sha256_hex(&tar_bytes);
        let (media_type, blob_bytes) = match layer.compression {
            Compression::PlainTar => ("application/vnd.oci.image.layer.v1.tar", tar_bytes.clone()),
            Compression::Gzip => (
                "application/vnd.oci.image.layer.v1.tar+gzip",
                gzip_bytes(&tar_bytes)?,
            ),
            Compression::GzipTruncated => {
                let mut gz = gzip_bytes(&tar_bytes)?;
                let cut = gz.len().saturating_sub(16).max(4);
                gz.truncate(cut);
                ("application/vnd.oci.image.layer.v1.tar+gzip", gz)
            }
            Compression::GarbageGzip => (
                "application/vnd.oci.image.layer.v1.tar+gzip",
                gzip_bytes(b"this is definitely not a tar archive")?,
            ),
        };
        let actual_digest = sha256_hex(&blob_bytes);
        let descriptor_digest = layer
            .force_descriptor_digest
            .clone()
            .unwrap_or_else(|| actual_digest.clone());
        // Write the blob under its ACTUAL digest (the store resolves by
        // descriptor digest; for tampering tests we also write under the
        // forced name so resolution succeeds but verification fails).
        write_blob(&blob_dir, &actual_digest, &blob_bytes)?;
        if descriptor_digest != actual_digest {
            write_blob(&blob_dir, &descriptor_digest, &blob_bytes)?;
        }
        diff_ids.push(layer.force_diff_id.clone().unwrap_or(uncompressed_digest));
        layer_descriptors.push(Descriptor {
            media_type: media_type.into(),
            digest: descriptor_digest,
            size: blob_bytes.len() as i64,
            actual_digest,
        });
    }

    let config_json = serde_json::json!({
        "os": "linux",
        "architecture": "amd64",
        "rootfs": {
            "type": "layers",
            "diff_ids": diff_ids,
        },
        "config": {},
    });
    let config_bytes = serde_json::to_vec_pretty(&config_json)?;
    let config_digest = sha256_hex(&config_bytes);
    write_blob(&blob_dir, &config_digest, &config_bytes)?;

    let manifest_json = serde_json::json!({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": config_bytes.len(),
        },
        "layers": layer_descriptors
            .iter()
            .map(|d| serde_json::json!({
                "mediaType": d.media_type,
                "digest": d.digest,
                "size": d.size,
            }))
            .collect::<Vec<_>>(),
    });
    let manifest_bytes = serde_json::to_vec_pretty(&manifest_json)?;
    let manifest_digest = sha256_hex(&manifest_bytes);
    write_blob(&blob_dir, &manifest_digest, &manifest_bytes)?;

    let index_json = serde_json::json!({
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": manifest_bytes.len(),
            }
        ],
    });
    fs::write(
        dir.join("index.json"),
        serde_json::to_vec_pretty(&index_json)?,
    )?;
    fs::write(
        dir.join("oci-layout"),
        br#"{"imageLayoutVersion": "1.0.0"}"#,
    )?;

    Ok(BuiltImage {
        manifest_digest,
        config_digest,
        layers: layer_descriptors,
    })
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Descriptor {
    #[serde(rename = "mediaType")]
    pub media_type: String,
    pub digest: String,
    pub size: i64,
    #[serde(skip)]
    pub actual_digest: String,
}

#[derive(Debug, Clone)]
pub struct BuiltImage {
    pub manifest_digest: String,
    pub config_digest: String,
    pub layers: Vec<Descriptor>,
}

fn write_blob(dir: &Path, digest: &str, bytes: &[u8]) -> anyhow::Result<()> {
    let hexpart = digest
        .strip_prefix("sha256:")
        .ok_or_else(|| anyhow::anyhow!("bad digest"))?;
    let p = dir.join(hexpart);
    fs::write(p, bytes)?;
    Ok(())
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    format!("sha256:{}", hex::encode(h.finalize()))
}

fn gzip_bytes(data: &[u8]) -> anyhow::Result<Vec<u8>> {
    use flate2::write::GzEncoder;
    let mut enc = GzEncoder::new(Vec::new(), flate2::Compression::default());
    enc.write_all(data)?;
    Ok(enc.finish()?)
}

/// Serialize [`RawEntry`]s into an (uncompressed) ustar tar.
fn build_tar(entries: &[RawEntry]) -> anyhow::Result<Vec<u8>> {
    let mut ar = tar::Builder::new(Vec::new());
    for e in entries {
        append_entry(&mut ar, e)?;
    }
    // Explicit terminator (two zero blocks) so hostile RawTar injections still
    // form a structurally complete archive the parser will read to EOF.
    ar.get_mut().write_all(&tar_terminator())?;
    Ok(ar.into_inner()?)
}

fn append_entry(ar: &mut tar::Builder<Vec<u8>>, e: &RawEntry) -> anyhow::Result<()> {
    let mut header = tar::Header::new_ustar();
    let path = std::path::Path::new(e.path.as_str());
    match &e.kind {
        RawKind::Dir => {
            let mut p = e.path.clone();
            if !p.ends_with('/') {
                p.push('/');
            }
            header.set_path(p)?;
            header.set_size(0);
            header.set_mode(0o755);
            header.set_entry_type(tar::EntryType::Directory);
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, std::io::empty())?;
        }
        RawKind::File { mode, contents } => {
            header.set_path(path)?;
            header.set_size(contents.len() as u64);
            header.set_mode(*mode);
            header.set_entry_type(tar::EntryType::Regular);
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, contents.as_slice())?;
        }
        RawKind::Symlink { target } => {
            header.set_path(path)?;
            header.set_link_name(target)?;
            header.set_size(0);
            header.set_mode(0o777);
            header.set_entry_type(tar::EntryType::Symlink);
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, std::io::empty())?;
        }
        RawKind::Hardlink { target } => {
            header.set_path(path)?;
            header.set_link_name(target)?;
            header.set_size(0);
            header.set_mode(0o644);
            header.set_entry_type(tar::EntryType::Link);
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, std::io::empty())?;
        }
        RawKind::Char { major, minor } => {
            header.set_path(path)?;
            header.set_size(0);
            header.set_mode(0o644);
            header.set_entry_type(tar::EntryType::Char);
            header.set_device_major(*major)?;
            header.set_device_minor(*minor)?;
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, std::io::empty())?;
        }
        RawKind::Whiteout { .. } | RawKind::OpaqueDir => {
            // Whiteouts are plain regular files with the special name and
            // zero/empty contents (the presence of the name is the marker).
            header.set_path(path)?;
            header.set_size(0);
            header.set_mode(0o644);
            header.set_entry_type(tar::EntryType::Regular);
            header.set_mtime(0);
            header.set_cksum();
            ar.append(&header, std::io::empty())?;
        }
        RawKind::RawTar(bytes) => {
            // Verbatim injection into the tar stream (hostile header/body).
            ar.get_mut().write_all(bytes)?;
        }
    }
    Ok(())
}

/// Construct a raw tar member (header + body) for names the safe builder
/// refuses (path traversal, absolute paths). The 512-byte ustar header is
/// filled by hand so the tar crate's `..` guard in `set_path` is bypassed and
/// the hostile name reaches the unpacker exactly as an attacker would write
/// it. Returns header + body padded to 512.
pub fn raw_tar_member(
    path: &str,
    kind: tar::EntryType,
    body: &[u8],
    link: Option<&str>,
) -> Vec<u8> {
    let mut h = vec![0u8; 512];
    let put = |buf: &mut [u8], off: usize, data: &[u8]| {
        let n = data.len().min(buf.len() - off);
        buf[off..off + n].copy_from_slice(&data[..n]);
    };
    let put_octal = |buf: &mut [u8], off: usize, len: usize, mut value: u64| {
        // Field width `len`; fill as octal digits followed by NUL/space.
        let mut digits = Vec::with_capacity(len);
        if value == 0 {
            digits.push(b'0');
        }
        while value > 0 {
            digits.push(b'0' + (value & 7) as u8);
            value >>= 3;
        }
        digits.reverse();
        let field_len = len - 1;
        for i in 0..field_len.saturating_sub(digits.len()) {
            buf[off + i] = b'0';
        }
        let start = off + field_len.saturating_sub(digits.len());
        buf[start..start + digits.len()].copy_from_slice(&digits);
        buf[off + len - 1] = 0;
    };

    put(&mut h, 0, path.as_bytes()); // name[0..100]
    put_octal(&mut h, 100, 8, 0o644); // mode
    put_octal(&mut h, 108, 8, 0); // uid
    put_octal(&mut h, 116, 8, 0); // gid
    put_octal(&mut h, 124, 12, body.len() as u64); // size
    put_octal(&mut h, 136, 12, 0); // mtime
                                   // checksum field (148..156) left as spaces while summing
    for b in &mut h[148..156] {
        *b = b' ';
    }
    h[156] = typeflag_byte(kind);
    if let Some(l) = link {
        put(&mut h, 157, l.as_bytes()); // linkname[157..257]
    }
    put(&mut h, 257, b"ustar\0"); // magic
    put(&mut h, 263, b"00"); // version
                             // devmajor/devminor at 329..337 / 337..345 (for the char-device case)
    if matches!(kind, tar::EntryType::Char | tar::EntryType::Block) {
        put_octal(&mut h, 329, 8, 1);
        put_octal(&mut h, 337, 8, 3);
    }
    let chksum: u32 = h.iter().map(|&b| b as u32).sum();
    let cs = format!("{chksum:06o}\0 ");
    put(&mut h, 148, cs.as_bytes());

    let mut out = h;
    out.extend_from_slice(body);
    let rem = (512 - (body.len() % 512)) % 512;
    out.extend(std::iter::repeat_n(0u8, rem));
    out
}

fn typeflag_byte(kind: tar::EntryType) -> u8 {
    match kind {
        tar::EntryType::Regular | tar::EntryType::Continuous => b'0',
        tar::EntryType::Link => b'1',
        tar::EntryType::Symlink => b'2',
        tar::EntryType::Char => b'3',
        tar::EntryType::Block => b'4',
        tar::EntryType::Directory => b'5',
        tar::EntryType::Fifo => b'6',
        other => other.as_byte(),
    }
}

/// Two zero blocks terminating a tar stream.
pub fn tar_terminator() -> Vec<u8> {
    vec![0u8; 1024]
}
