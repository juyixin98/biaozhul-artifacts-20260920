//! Mark-and-sweep garbage collection.
//!
//! Correctness argument for "publish root concurrently with GC":
//!
//! Every state mutation in this store — `set_root`, `delete_root`,
//! `put_blob`, `put_manifest`, upload staging/completion — and the entire
//! mark-and-sweep below run while holding the *same* `std::sync::Mutex`.
//! A GC pass therefore observes a single consistent snapshot of roots and
//! staged uploads, and no root can be published "in the middle" of a sweep.
//! An object reachable from any root visible at the start of the pass is
//! marked and cannot be swept; a root published after the pass starts is
//! simply accounted for by the next pass. This trades some concurrency for a
//! safety guarantee that is trivial to reason about: **an object reachable
//! from a live root is never collected.**

use std::collections::HashSet;
use std::fs;

use serde::Serialize;

use crate::store::{Manifest, Store};

/// Outcome of one GC pass.
#[derive(Clone, Debug, Serialize)]
pub struct GcReport {
    /// Monotonic generation of this pass.
    pub generation: u64,
    /// Objects marked reachable (from roots + live staged uploads).
    pub marked: usize,
    /// Objects deleted by this pass.
    pub swept: usize,
    /// Hashes deleted by this pass (bounded by store size).
    pub swept_hashes: Vec<String>,
    /// Staged uploads dropped because their retention window elapsed.
    pub expired_uploads: Vec<String>,
    /// Total objects remaining after the pass.
    pub remaining: usize,
}

impl Store {
    /// Run one mark-and-sweep pass.
    ///
    /// Retained:
    ///   * everything reachable from any current root;
    ///   * everything reachable from a staged upload still inside its
    ///     retention window ("incomplete uploads have their own retention").
    ///
    /// Swept: every other object. Staged uploads past their retention window
    /// are discarded first, so their closure becomes collectable.
    pub fn gc(&self) -> GcReport {
        let mut s = self.inner.lock().unwrap();
        let now = Self::now_unix();

        // 1. Expire staged uploads whose retention window has elapsed.
        let expired: Vec<String> = s
            .uploads
            .values()
            .filter(|u| now >= u.created_unix.saturating_add(u.retention_secs))
            .map(|u| u.id.clone())
            .collect();
        for id in &expired {
            s.uploads.remove(id);
        }

        // 2. Mark: BFS from roots and live staged uploads.
        let seeds: Vec<String> = s
            .roots
            .values()
            .cloned()
            .chain(s.uploads.values().map(|u| u.manifest.clone()))
            .collect();
        let mut marked: HashSet<String> = HashSet::new();
        let mut stack = seeds;
        while let Some(h) = stack.pop() {
            if !marked.insert(h.clone()) {
                continue;
            }
            if s.manifests.contains(&h) {
                if let Ok(bytes) = fs::read(self.object_path(&h)) {
                    if let Ok(m) = serde_json::from_slice::<Manifest>(&bytes) {
                        stack.extend(m.manifests.iter().cloned());
                        stack.extend(m.blobs.iter().cloned());
                    }
                }
            }
        }

        // 3. Sweep: delete every unmarked object.
        let mut swept_hashes: Vec<String> = s
            .objects
            .iter()
            .filter(|h| !marked.contains(*h))
            .cloned()
            .collect();
        swept_hashes.sort();
        for h in &swept_hashes {
            let _ = fs::remove_file(self.object_path(h));
            s.objects.remove(h);
            s.manifests.remove(h);
        }

        s.gc_gen += 1;
        let report = GcReport {
            generation: s.gc_gen,
            marked: marked.len(),
            swept: swept_hashes.len(),
            swept_hashes,
            expired_uploads: expired,
            remaining: s.objects.len(),
        };
        drop(s);
        // Persist the post-GC state (outside the lock is fine: persist()
        // re-acquires it and writes a consistent snapshot).
        let _ = self.persist();
        report
    }
}
