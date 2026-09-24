//! Generates local OCI image fixtures for demo / acceptance testing.
//!
//!     cargo run --example make_fixtures -- [TARGET_DIR]
//!
//! Writes these images into `<TARGET_DIR>/images` (default `./workdir`):
//!
//! | image                 | demonstrates                                              |
//! |-----------------------|-----------------------------------------------------------|
//! | demo-app              | 3 layers: base, overlay, delete+recreate, opaque dir      |
//! | evil-traversal        | `../` path entry — rebuild must return 422                |
//! | evil-symlink          | escaping absolute symlink — rebuild must return 422       |
//! | evil-device           | character device node — rebuild must return 422           |
//! | corrupt-layer         | bit-flipped gzip whose digest still matches the manifest* |
//!
//! (*) `corrupt-layer` is generated with matching digests by recomputing the
//! manifest after corruption, i.e. it models an internally-corrupt but
//! correctly-addressed blob; digest-tamper scenarios are covered by the
//! test suite instead.
//!
//! All digests are real SHA-256 computed over the exact stored bytes.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use flate2::write::GzEncoder;
use flate2::Compression;
use sha2::{Digest, Sha256};

// ---- tiny tar writer (whitelist-conformant entries only) ----------------

struct TarBuilder {
    out: Vec<u8>,
}

impl TarBuilder {
    fn new() -> Self {
        Self { out: Vec::new() }
    }

    fn header(&mut self, path: &str, typeflag: u8, mode: u32, size: u64, link: &str) {
        let mut h = vec![0u8; 512];
        assert!(path.len() <= 100 && link.len() <= 100);
        h[..path.len()].copy_from_slice(path.as_bytes());
        octal(&mut h[100..108], mode as u64);
        octal(&mut h[108..116], 0);
        octal(&mut h[116..124], 0);
        octal(&mut h[124..136], size);
        octal(&mut h[136..148], 0);
        for b in &mut h[148..156] {
            *b = b' ';
        }
        h[156] = typeflag;
        h[157..157 + link.len()].copy_from_slice(link.as_bytes());
        h[257..263].copy_from_slice(b"ustar\0");
        h[263..265].copy_from_slice(b"00");
        let sum: u64 = h.iter().map(|&b| b as u64).sum();
        let cksum = format!("{sum:06o}\0 ");
        h[148..156].copy_from_slice(cksum.as_bytes());
        self.out.extend_from_slice(&h);
    }

    fn dir(mut self, path: &str, mode: u32) -> Self {
        let p = if path.ends_with('/') {
            path.to_string()
        } else {
            format!("{path}/")
        };
        self.header(&p, b'5', mode, 0, "");
        self
    }

    fn file(mut self, path: &str, data: &[u8], mode: u32) -> Self {
        self.header(path, b'0', mode, data.len() as u64, "");
        self.out.extend_from_slice(data);
        let pad = (512 - (data.len() % 512)) % 512;
        self.out.extend(std::iter::repeat_n(0u8, pad));
        self
    }

    fn symlink(mut self, link: &str, target: &str) -> Self {
        self.header(link, b'2', 0o777, 0, target);
        self
    }

    fn raw_char(mut self, path: &str) -> Self {
        // Hand-made char device header (major 1, minor 3 = /dev/null-like).
        let mut h = vec![0u8; 512];
        h[..path.len()].copy_from_slice(path.as_bytes());
        octal(&mut h[100..108], 0o600);
        octal(&mut h[108..116], 0);
        octal(&mut h[116..124], 0);
        octal(&mut h[124..136], 0);
        octal(&mut h[136..148], 0);
        for b in &mut h[148..156] {
            *b = b' ';
        }
        h[156] = b'3'; // char device
        octal(&mut h[329..337], 1); // devmajor
        octal(&mut h[337..345], 3); // devminor
        h[257..263].copy_from_slice(b"ustar\0");
        h[263..265].copy_from_slice(b"00");
        let sum: u64 = h.iter().map(|&b| b as u64).sum();
        let cksum = format!("{sum:06o}\0 ");
        h[148..156].copy_from_slice(cksum.as_bytes());
        self.out.extend_from_slice(&h);
        self
    }

    /// Raw path header without any validation (malicious fixture).
    fn raw_traversal_file(mut self, path: &str, data: &[u8]) -> Self {
        self.header(path, b'0', 0o644, data.len() as u64, "");
        self.out.extend_from_slice(data);
        let pad = (512 - (data.len() % 512)) % 512;
        self.out.extend(std::iter::repeat_n(0u8, pad));
        self
    }

    fn finish(mut self) -> Vec<u8> {
        self.out.extend([0u8; 1024]);
        self.out
    }
}

fn octal(field: &mut [u8], value: u64) {
    let width = field.len();
    let s = format!("{:0width$o}", value, width = width - 1);
    let b = s.as_bytes();
    field[..width - 1].copy_from_slice(&b[b.len() - (width - 1)..]);
    field[width - 1] = 0;
}

// ---- OCI layout assembly ------------------------------------------------

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

fn gzip(data: &[u8]) -> Vec<u8> {
    let mut e = GzEncoder::new(Vec::new(), Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

struct OciImage {
    root: PathBuf,
    blobs: PathBuf,
    layers: Vec<serde_json::Value>,
}

impl OciImage {
    fn create(base: &Path, name: &str) -> Self {
        let root = base.join(name);
        let blobs = root.join("blobs").join("sha256");
        fs::create_dir_all(&blobs).unwrap();
        fs::write(root.join("oci-layout"), r#"{"imageLayoutVersion":"1.0.0"}"#).unwrap();
        Self {
            root,
            blobs,
            layers: Vec::new(),
        }
    }

    fn add_layer_bytes(&mut self, stored: Vec<u8>, gzipped: bool) {
        let digest_hex = sha256_hex(&stored);
        let size = stored.len();
        fs::write(self.blobs.join(&digest_hex), stored).unwrap();
        let media = if gzipped {
            "application/vnd.oci.image.layer.v1.tar+gzip"
        } else {
            "application/vnd.oci.image.layer.v1.tar"
        };
        self.layers.push(serde_json::json!({
            "mediaType": media,
            "digest": format!("sha256:{digest_hex}"),
            "size": size,
        }));
    }

    fn add_layer(&mut self, tar: Vec<u8>) {
        self.add_layer_bytes(gzip(&tar), true);
    }

    fn finalize(self) {
        let config = serde_json::json!({
            "architecture": "amd64",
            "os": "linux",
            "rootfs": { "type": "layers", "diff_ids": [] },
            "config": { "Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"] }
        });
        let config_bytes = serde_json::to_vec(&config).unwrap();
        let config_hex = sha256_hex(&config_bytes);
        fs::write(self.blobs.join(&config_hex), &config_bytes).unwrap();

        let manifest = serde_json::json!({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": format!("sha256:{config_hex}"),
                "size": config_bytes.len(),
            },
            "layers": self.layers,
        });
        let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
        let manifest_hex = sha256_hex(&manifest_bytes);
        fs::write(self.blobs.join(&manifest_hex), &manifest_bytes).unwrap();

        let index = serde_json::json!({
            "schemaVersion": 2,
            "manifests": [{
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": format!("sha256:{manifest_hex}"),
                "size": manifest_bytes.len(),
            }],
        });
        fs::write(
            self.root.join("index.json"),
            serde_json::to_vec_pretty(&index).unwrap(),
        )
        .unwrap();
    }
}

// ---- fixtures -----------------------------------------------------------

fn build_demo_app(base: &Path) {
    let mut img = OciImage::create(base, "demo-app");

    // Layer 0: base filesystem
    img.add_layer(
        TarBuilder::new()
            .dir("etc", 0o755)
            .dir("app", 0o755)
            .dir("app/logs", 0o755)
            .file("etc/hostname", b"demo-base\n", 0o644)
            .file("app/version", b"1.0\n", 0o644)
            .file("app/logs/old.log", b"stale log line\n", 0o644)
            .symlink("app/current", "version")
            .finish(),
    );

    // Layer 1: overwrite same path at multiple layers + new file
    img.add_layer(
        TarBuilder::new()
            .file("etc/hostname", b"demo-patched\n", 0o644)
            .file("app/version", b"2.0\n", 0o644)
            .file("app/main.sh", b"#!/bin/sh\necho hi\n", 0o755)
            .finish(),
    );

    // Layer 2: delete-then-recreate (app/version removed, comes back new),
    // plus an opaque directory that wipes app/logs.
    img.add_layer(
        TarBuilder::new()
            .dir("app", 0o755)
            .file("app/.wh.version", &[], 0o000)
            .file("app/version", b"3.0\n", 0o644)
            .dir("app/logs", 0o755)
            .file("app/logs/.wh..wh..opq", &[], 0o000)
            .file("app/logs/fresh.log", b"fresh\n", 0o644)
            .finish(),
    );

    img.finalize();
}

fn build_evil_traversal(base: &Path) {
    let mut img = OciImage::create(base, "evil-traversal");
    let tar = TarBuilder::new()
        .file("hello.txt", b"harmless\n", 0o644)
        .raw_traversal_file("../escape.txt", b"escaped via parent dir\n")
        .finish();
    img.add_layer(tar);
    img.finalize();
}

fn build_evil_symlink(base: &Path) {
    let mut img = OciImage::create(base, "evil-symlink");
    let tar = TarBuilder::new()
        .dir("bin", 0o755)
        // absolute target pointing outside the root
        .symlink("bin/sh", "/bin/sh")
        .finish();
    img.add_layer(tar);
    img.finalize();
}

fn build_evil_device(base: &Path) {
    let mut img = OciImage::create(base, "evil-device");
    let tar = TarBuilder::new()
        .dir("dev", 0o755)
        .raw_char("dev/nullish")
        .finish();
    img.add_layer(tar);
    img.finalize();
}

fn build_corrupt_layer(base: &Path) {
    // Build a valid gzip layer, flip one compressed byte, then wire up the
    // OCI metadata with the digest of the *corrupt* bytes — so the failure
    // is detected by gzip integrity validation rather than digest mismatch.
    let mut img = OciImage::create(base, "corrupt-layer");
    let good_tar = TarBuilder::new().file("ok", b"data\n", 0o644).finish();
    let mut bad = gzip(&good_tar);
    // Flip a byte in the deflate body (after the 10-byte gzip header).
    bad[15] ^= 0x5A;
    img.add_layer_bytes(bad, true);
    img.finalize();
}

fn main() {
    let target = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("./workdir"));
    let images = target.join("images");
    fs::create_dir_all(&images).unwrap();

    let mut made = Vec::new();
    for (name, builder) in [
        ("demo-app", build_demo_app as fn(&Path)),
        ("evil-traversal", build_evil_traversal),
        ("evil-symlink", build_evil_symlink),
        ("evil-device", build_evil_device),
        ("corrupt-layer", build_corrupt_layer),
    ] {
        builder(&images);
        made.push(name);
    }

    println!("created fixtures under {}:", images.display());
    for name in made {
        println!("  - {name}");
    }
}
