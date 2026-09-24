#![allow(dead_code)]
//! Common helpers for integration tests.

use std::path::PathBuf;

use fixture_maker::{build_image, LayerSpec};
use oci_unpack::{Builds, Limits, RebuildResult, Store};

pub struct Env {
    pub root: tempfile::TempDir,
    pub fixtures: PathBuf,
    pub builds: PathBuf,
}

impl Env {
    pub fn new() -> Self {
        let root = tempfile::tempdir().expect("tempdir");
        let fixtures = root.path().join("fixtures");
        let builds = root.path().join("builds");
        std::fs::create_dir_all(&fixtures).unwrap();
        Env {
            root,
            fixtures,
            builds,
        }
    }

    pub fn make(&self, name: &str, layers: &[LayerSpec]) {
        build_image(self.fixtures.join(name), layers)
            .unwrap_or_else(|e| panic!("build fixture {name}: {e}"));
    }

    pub fn rebuild(&self, name: &str) -> oci_unpack::Result<RebuildResult> {
        self.rebuild_with(name, Limits::default())
    }

    pub fn rebuild_with(&self, name: &str, limits: Limits) -> oci_unpack::Result<RebuildResult> {
        let store = Store::new(&self.fixtures);
        let builds = Builds::new(&self.builds).unwrap();
        oci_unpack::rebuild(&store, &builds, name, &limits)
    }

    /// Absolute path of a published file/dir inside the rootfs.
    pub fn rootfs(&self, name: &str, rel: &str) -> PathBuf {
        self.builds
            .join(name)
            .join("latest")
            .join("rootfs")
            .join(rel)
    }

    pub fn published(&self, name: &str) -> bool {
        self.builds.join(name).join("latest").exists()
    }

    pub fn read(&self, name: &str, rel: &str) -> String {
        std::fs::read_to_string(self.rootfs(name, rel))
            .unwrap_or_else(|e| panic!("read {name}:{rel}: {e}"))
    }

    pub fn file_kind(&self, name: &str, rel: &str) -> String {
        let p = self.rootfs(name, rel);
        let meta = std::fs::symlink_metadata(&p).expect("metadata");
        if meta.file_type().is_symlink() {
            "symlink".into()
        } else if meta.is_dir() {
            "dir".into()
        } else {
            "file".into()
        }
    }
}
