//! Test fixture builder: constructs *real* OCI image layouts on disk with
//! real tar (optionally gzip) layers and real SHA-256 content digests.
#![allow(dead_code)]

use std::collections::HashMap;
use std::fs;
use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};

use flate2::write::GzEncoder;
use flate2::Compression;
use sha2::{Digest, Sha256};

/// One tar entry specification.
#[derive(Clone)]
pub enum Entry {
    Dir(&'static str),
    File(&'static str, Vec<u8>, u32),
    Symlink {
        link: &'static str,
        target: &'static str,
    },
    Hardlink {
        link: &'static str,
        target: &'static str,
    },
    Char {
        link: &'static str,
        major: u32,
        minor: u32,
    },
    Fifo(&'static str),
    /// Hand-crafted tar header with no path validation — used to emulate
    /// malicious archives (traversal names, absolute names, devices).
    Raw(RawEntry),
}

#[derive(Clone)]
pub struct RawEntry {
    pub name: String,
    pub data: Vec<u8>,
    pub mode: u32,
    pub typeflag: u8,
    pub linkname: Option<String>,
}

impl RawEntry {
    pub fn traversal_file(name: &str, data: &[u8]) -> Entry {
        Entry::Raw(RawEntry {
            name: name.to_string(),
            data: data.to_vec(),
            mode: 0o644,
            typeflag: b'0',
            linkname: None,
        })
    }
}

pub struct Layer {
    pub entries: Vec<Entry>,
    pub gzip: bool,
    pub corrupt_gzip: bool,
}

impl Layer {
    pub fn new(entries: Vec<Entry>) -> Self {
        Self {
            entries,
            gzip: true,
            corrupt_gzip: false,
        }
    }
    pub fn plain(mut self) -> Self {
        self.gzip = false;
        self
    }
    pub fn corrupt(mut self) -> Self {
        self.gzip = true;
        self.corrupt_gzip = true;
        self
    }
}

pub struct BuiltImage {
    pub root: PathBuf,
    /// layer digest strings in order
    pub layer_digests: Vec<String>,
    /// content digest of each file in the *last* occurrence of a File entry,
    /// keyed by path — convenience for assertions
    pub file_hashes: HashMap<String, String>,
}

fn append_octal(field: &mut [u8], value: u64) {
    let width = field.len();
    let s = format!("{:0width$o}", value, width = width - 1);
    let bytes = s.as_bytes();
    field[..width - 1].copy_from_slice(&bytes[bytes.len() - (width - 1)..]);
    field[width - 1] = 0;
}

/// Serialise one hand-crafted (unvalidated) 512-byte ustar header + data.
fn append_raw_entry(out: &mut Vec<u8>, e: &RawEntry) {
    assert!(
        e.name.len() <= 100,
        "raw name too long for ustar short field"
    );
    let linkname = e.linkname.as_deref().unwrap_or("");
    assert!(linkname.len() <= 100, "raw linkname too long");

    let mut h = vec![0u8; 512];
    h[..e.name.len()].copy_from_slice(e.name.as_bytes());
    append_octal(&mut h[100..108], e.mode as u64);
    append_octal(&mut h[108..116], 0); // uid
    append_octal(&mut h[116..124], 0); // gid
    append_octal(&mut h[124..136], e.data.len() as u64);
    append_octal(&mut h[136..148], 0); // mtime
                                       // checksum placeholder: eight spaces
    for b in &mut h[148..156] {
        *b = b' ';
    }
    h[156] = e.typeflag;
    h[157..157 + linkname.len()].copy_from_slice(linkname.as_bytes());
    h[257..263].copy_from_slice(b"ustar\0");
    h[263..265].copy_from_slice(b"00");
    // checksum: sum of all header bytes, six octal digits + NUL + space
    let sum: u64 = h.iter().map(|&b| b as u64).sum();
    let cksum = format!("{sum:06o}\0 ");
    h[148..156].copy_from_slice(cksum.as_bytes());

    out.extend_from_slice(&h);
    out.extend_from_slice(&e.data);
    let pad = (512 - (e.data.len() % 512)) % 512;
    out.extend(std::iter::repeat_n(0u8, pad));
}

fn tar_bytes(entries: &[Entry]) -> Vec<u8> {
    let mut builder = tar::Builder::new(Vec::new());
    let mut raw: Vec<RawEntry> = Vec::new();
    for e in entries {
        match e {
            Entry::Raw(r) => raw.push(r.clone()),
            Entry::Dir(p) => {
                let mut h = tar::Header::new_gnu();
                h.set_path(p).unwrap();
                h.set_entry_type(tar::EntryType::Directory);
                h.set_mode(0o755);
                h.set_size(0);
                h.set_cksum();
                builder.append(&h, &mut std::io::empty()).unwrap();
            }
            Entry::File(p, data, mode) => {
                let mut h = tar::Header::new_gnu();
                h.set_path(p).unwrap();
                h.set_entry_type(tar::EntryType::Regular);
                h.set_mode(*mode);
                h.set_size(data.len() as u64);
                h.set_cksum();
                builder.append(&h, Cursor::new(data)).unwrap();
            }
            Entry::Symlink { link, target } => {
                let mut h = tar::Header::new_gnu();
                h.set_path(link).unwrap();
                h.set_entry_type(tar::EntryType::Symlink);
                h.set_link_name(target).unwrap();
                h.set_mode(0o777);
                h.set_size(0);
                h.set_cksum();
                builder.append(&h, &mut std::io::empty()).unwrap();
            }
            Entry::Hardlink { link, target } => {
                let mut h = tar::Header::new_gnu();
                h.set_path(link).unwrap();
                h.set_entry_type(tar::EntryType::Link);
                h.set_link_name(target).unwrap();
                h.set_mode(0o644);
                h.set_size(0);
                h.set_cksum();
                builder.append(&h, &mut std::io::empty()).unwrap();
            }
            Entry::Char { link, major, minor } => {
                let mut h = tar::Header::new_gnu();
                h.set_path(link).unwrap();
                h.set_entry_type(tar::EntryType::Char);
                h.set_device_major(*major).unwrap();
                h.set_device_minor(*minor).unwrap();
                h.set_mode(0o600);
                h.set_size(0);
                h.set_cksum();
                builder.append(&h, &mut std::io::empty()).unwrap();
            }
            Entry::Fifo(p) => {
                let mut h = tar::Header::new_gnu();
                h.set_path(p).unwrap();
                h.set_entry_type(tar::EntryType::Fifo);
                h.set_mode(0o644);
                h.set_size(0);
                h.set_cksum();
                builder.append(&h, &mut std::io::empty()).unwrap();
            }
        }
    }
    let mut out = builder.into_inner().unwrap();
    // tar-rs appended its two trailing zero blocks; strip them so hand-crafted
    // entries still appear before the end-of-archive marker, then re-add it.
    if out.len() >= 1024 && out[out.len() - 1024..].iter().all(|&b| b == 0) {
        out.truncate(out.len() - 1024);
    }
    for r in &raw {
        append_raw_entry(&mut out, r);
    }
    out.extend([0u8; 1024]);
    out
}

fn gzip_bytes(data: &[u8]) -> Vec<u8> {
    let mut enc = GzEncoder::new(Vec::new(), Compression::default());
    enc.write_all(data).unwrap();
    enc.finish().unwrap()
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

/// Assemble an OCI image fixture from layers.
///
/// * `tamper_layer[i] = Some(bytes)` replaces the *content* of layer blob i
///   while keeping the manifest digest reference — used to create digest
///   mismatches.
pub fn build_image(
    images_dir: &Path,
    name: &str,
    layers: Vec<Layer>,
    tamper_layer: Option<HashMap<usize, Vec<u8>>>,
) -> BuiltImage {
    let root = images_dir.join(name);
    let blob_dir = root.join("blobs").join("sha256");
    fs::create_dir_all(&blob_dir).unwrap();
    fs::write(root.join("oci-layout"), r#"{"imageLayoutVersion":"1.0.0"}"#).unwrap();

    let mut layer_digests = Vec::new();
    let mut file_hashes = HashMap::new();
    let mut manifest_layers = Vec::new();

    for (i, layer) in layers.iter().enumerate() {
        let tar = tar_bytes(&layer.entries);
        let mut stored = if layer.gzip { gzip_bytes(&tar) } else { tar };
        if layer.corrupt_gzip {
            // Flip bytes in the middle and truncate the CRC trailer.
            let mid = stored.len() / 2;
            stored[mid] ^= 0xFF;
            stored.truncate(stored.len().saturating_sub(4));
        }
        let media = if layer.gzip {
            "application/vnd.oci.image.layer.v1.tar+gzip"
        } else {
            "application/vnd.oci.image.layer.v1.tar"
        };
        let digest_hex = sha256_hex(&stored);
        let digest = format!("sha256:{digest_hex}");
        if let Some(replacement) = tamper_layer.as_ref().and_then(|m| m.get(&i)) {
            stored = replacement.clone();
        }
        fs::write(blob_dir.join(&digest_hex), &stored).unwrap();
        layer_digests.push(digest.clone());
        manifest_layers.push(serde_json::json!({
            "mediaType": media,
            "digest": digest,
            "size": stored.len(),
        }));

        for e in &layer.entries {
            if let Entry::File(p, data, _) = e {
                file_hashes.insert((*p).to_string(), format!("sha256:{}", sha256_hex(data)));
            }
        }
    }

    // image config (minimal but real JSON; digest referenced by manifest)
    let config = serde_json::json!({
        "architecture": "amd64",
        "os": "linux",
        "rootfs": { "type": "layers", "diff_ids": [] },
        "config": {}
    });
    let config_bytes = serde_json::to_vec(&config).unwrap();
    let config_hex = sha256_hex(&config_bytes);
    fs::write(blob_dir.join(&config_hex), &config_bytes).unwrap();

    let manifest = serde_json::json!({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": format!("sha256:{config_hex}"),
            "size": config_bytes.len(),
        },
        "layers": manifest_layers,
    });
    let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
    let manifest_hex = sha256_hex(&manifest_bytes);
    fs::write(blob_dir.join(&manifest_hex), &manifest_bytes).unwrap();

    let index = serde_json::json!({
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": format!("sha256:{manifest_hex}"),
            "size": manifest_bytes.len(),
        }],
    });
    fs::write(
        root.join("index.json"),
        serde_json::to_vec_pretty(&index).unwrap(),
    )
    .unwrap();

    BuiltImage {
        root,
        layer_digests,
        file_hashes,
    }
}

pub fn tmp_workdir() -> tempfile::TempDir {
    tempfile::tempdir().unwrap()
}

pub fn workdir_for(base: &Path) -> oci_unpack::Workdir {
    let w = oci_unpack::Workdir::new(base);
    fs::create_dir_all(w.images_dir()).unwrap();
    fs::create_dir_all(w.roots_dir()).unwrap();
    w
}

/// Read a file from the published rootfs.
pub fn read_rootfs(w: &oci_unpack::Workdir, image: &str, rel: &str) -> Option<Vec<u8>> {
    let root = w.published_root(image).ok().flatten()?;
    let p = root.join(rel);
    fs::read(p).ok()
}

pub fn exists_in_rootfs(w: &oci_unpack::Workdir, image: &str, rel: &str) -> bool {
    match w.published_root(image) {
        Ok(Some(root)) => root.join(rel).symlink_metadata().is_ok(),
        _ => false,
    }
}
