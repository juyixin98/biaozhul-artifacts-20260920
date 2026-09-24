//! Filesystem application of a conflict-free plan.
//!
//! The merge is staged in a sibling temporary directory inside the base
//! directory (same filesystem), then atomically `rename(2)`d into its final
//! directory. Any I/O failure aborts and removes the staging area; the final
//! target tree only ever appears complete.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

use crate::model::{EntryKind, Plan, PlannedNode};

/// Apply the plan. On success returns the absolute(ish) final root path.
///
/// `final_root` must not exist yet. Identical-content groups are stored once
/// as blobs and hard-linked (copy fallback) into the tree.
pub fn apply_plan(base_dir: &Path, merge_id: &str, plan: &Plan) -> io::Result<PathBuf> {
    let staging = base_dir.join(format!(".merge_staging.{merge_id}"));
    let blob_dir = base_dir.join(format!(".merge_blobs.{merge_id}"));
    let final_root = base_dir.join(merge_id);

    if final_root.exists() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            format!("target directory already exists: {}", final_root.display()),
        ));
    }

    let result = (|| -> io::Result<()> {
        fs::create_dir_all(&staging)?;
        fs::create_dir_all(&blob_dir)?;

        // Parents first, by depth.
        let mut ordered: Vec<&PlannedNode> = plan.nodes.iter().collect();
        ordered.sort_by_key(|n| (n.path.0.len(), n.path.as_str()));

        // Write one blob per distinct file hash.
        let mut blobs: BTreeMap<String, PathBuf> = BTreeMap::new();
        for n in &ordered {
            if n.kind == EntryKind::File {
                if let Some(hash) = &n.content_hash {
                    blobs
                        .entry(hash.clone())
                        .or_insert_with(|| blob_dir.join(hash));
                }
            }
        }
        for (hash, p) in &blobs {
            // Find the content for this hash from the first matching node.
            let content = ordered
                .iter()
                .find(|n| n.content_hash.as_deref() == Some(hash.as_str()))
                .and_then(|n| n.content.as_deref())
                .unwrap_or(&[]);
            fs::write(p, content)?;
        }

        for n in &ordered {
            let target = staging.join(n.path.as_str());
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            match n.kind {
                EntryKind::Dir => {
                    fs::create_dir_all(&target)?;
                }
                EntryKind::File => {
                    if let Some(hash) = &n.content_hash {
                        if let Some(blob) = blobs.get(hash) {
                            // Hard-link first (same filesystem, instant, saves
                            // space); fall back to a plain copy.
                            match fs::hard_link(blob, &target) {
                                Ok(()) => {}
                                Err(_) => {
                                    fs::copy(blob, &target)?;
                                }
                            }
                            continue;
                        }
                    }
                    fs::write(&target, n.content.as_deref().unwrap_or(&[]))?;
                }
                EntryKind::Symlink => {
                    let raw = n.link_target.as_deref().unwrap_or("");
                    symlink(raw, &target)?;
                }
            }
        }
        Ok(())
    })();

    match result {
        Ok(()) => {
            // Atomic publish. rename fails if final_root appeared meanwhile.
            match fs::rename(&staging, &final_root) {
                Ok(()) => {
                    let _ = fs::remove_dir_all(&blob_dir);
                    Ok(final_root)
                }
                Err(e) => {
                    let _ = fs::remove_dir_all(&staging);
                    let _ = fs::remove_dir_all(&blob_dir);
                    Err(e)
                }
            }
        }
        Err(e) => {
            let _ = fs::remove_dir_all(&staging);
            let _ = fs::remove_dir_all(&blob_dir);
            Err(e)
        }
    }
}
