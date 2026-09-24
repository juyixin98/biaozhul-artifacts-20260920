//! Disk-backed extendible hash index.
//!
//! # Extendible hashing recap
//!
//! Keys are hashed to 64 bits and the *low* `global_depth` bits select a slot
//! in a directory of `2^global_depth` page numbers. Each slot points at a
//! bucket page; buckets carry their own `local_depth` and are shared by
//! `2^(global_depth - local_depth)` slots.
//!
//! * A full bucket with `local_depth == global_depth` first *doubles* the
//!   directory (mirroring every slot) and then splits; a full shared bucket
//!   only splits. The split divides entries on bit `local_depth`.
//! * After deletions, an equal-depth buddy pair whose combined entries fit a
//!   single bucket merges; repeated merges cascade. When the two directory
//!   halves are identical the directory *shrinks* by one depth.
//!
//! # Crash model
//!
//! Structural moves never overwrite the only live copy of anything:
//!
//! * a split writes the two result buckets into freshly allocated pages and
//!   builds a freshly allocated directory run; the old bucket page and old
//!   run are freed only after the metadata commit;
//! * a merge likewise writes the merged bucket into a fresh page and a fresh
//!   directory run before dropping the two old buckets at commit;
//! * a shrink only flips the global depth in the header.
//!
//! The header additionally holds an [`Intent`] persisted *after* all page
//! reservations (so the intent header also carries the allocation result:
//! high-water/free-list updates) and *before* any pre-move page is touched.
//! Committing a split/merge takes two header writes:
//!
//! 1. adopt the new pages (global depth, bucket count, `dir_start`) with the
//!    intent still set and the free list excluding the now-dead pages;
//! 2. append the dead pages to the free list and clear the intent.
//!
//! On open, a live intent means the pre-move pages are still intact; recovery
//! rebuilds the new pages deterministically from them and re-runs the commit
//! (never trusting partial new pages). See [`Index::recover_split`] /
//! [`Index::recover_merge`].
use crate::error::{IndexError, Result};
use crate::hash::HashKind;
use crate::header::{decode as decode_header, encode as encode_header, Header, Intent, NULL_PAGE};
use crate::store::{
    decode_bucket, encode_bucket, encode_directory, read_directory, Bucket, Pager,
    DIR_SLOTS_PER_PAGE, PAGE_SIZE,
};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;

/// Hook invoked at named durability boundaries. Integration tests install a
/// hook that hard-exits the process to simulate a crash/power loss.
pub type CrashHook = dyn Fn(&str) + Send + Sync + 'static;

#[derive(Debug, Clone)]
pub struct Config {
    pub bucket_capacity: usize,
    pub max_depth: u32,
    pub hash: HashKind,
    pub key_max: usize,
    pub val_max: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bucket_capacity: 8,
            max_depth: 20,
            hash: HashKind::Fnv1a64,
            key_max: 128,
            val_max: 256,
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct BucketStat {
    pub page: u32,
    pub local_depth: u32,
    pub entries: usize,
    /// Number of directory slots referencing this bucket; always equals
    /// `2^(global_depth - local_depth)`.
    pub slot_refs: usize,
    pub keys: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub hash: String,
    pub global_depth: u32,
    pub directory_slots: usize,
    pub directory_pages: u32,
    pub bucket_count: u32,
    pub free_pages: usize,
    pub file_pages: u32,
    pub bucket_capacity: usize,
    pub max_depth: u32,
    pub key_max: usize,
    pub val_max: usize,
    pub recovered_intent: Option<String>,
    pub buckets: Vec<BucketStat>,
}

pub struct Index {
    pager: Pager,
    hash: HashKind,
    capacity: usize,
    max_depth: u32,
    key_max: usize,
    val_max: usize,

    global_depth: u32,
    bucket_count: u32,
    free_head: u32,
    high_water: u32,
    dir_start: u32,
    dir_pages: u32,
    directory: Vec<u32>,

    hook: Option<Arc<CrashHook>>,
    recovered_intent: Option<String>,
}

fn dir_pages_for(slots: usize) -> u32 {
    slots.div_ceil(DIR_SLOTS_PER_PAGE).max(1) as u32
}

impl Index {
    // -- lifecycle -------------------------------------------------------

    pub fn create(path: &Path, cfg: &Config) -> Result<Index> {
        Self::validate(cfg)?;
        let mut pager = Pager::create(path)?;

        let dir_start = 1u32;
        let bucket_page = 2u32;
        let high_water = 3u32;
        let directory = vec![bucket_page];

        pager.write_run(dir_start, &encode_directory(&directory))?;
        pager.write_page(
            bucket_page,
            &encode_bucket(&Bucket {
                local_depth: 0,
                entries: Vec::new(),
            }),
        )?;

        let header = Header {
            hash_kind_id: cfg.hash.id(),
            bucket_capacity: cfg.bucket_capacity as u16,
            global_depth: 0,
            bucket_count: 1,
            free_head: NULL_PAGE,
            high_water,
            max_depth: cfg.max_depth as u8,
            key_max: cfg.key_max as u32,
            val_max: cfg.val_max as u32,
            dir_start,
            dir_pages: 1,
            intent: None,
        };
        pager.write_header(&encode_header(&header))?;

        Ok(Index {
            pager,
            hash: cfg.hash,
            capacity: cfg.bucket_capacity,
            max_depth: cfg.max_depth,
            key_max: cfg.key_max,
            val_max: cfg.val_max,
            global_depth: 0,
            bucket_count: 1,
            free_head: NULL_PAGE,
            high_water,
            dir_start,
            dir_pages: 1,
            directory,
            hook: None,
            recovered_intent: None,
        })
    }

    pub fn open(path: &Path) -> Result<Index> {
        let mut pager = Pager::open(path)?;
        let raw = pager.read_header()?;
        let h = decode_header(&raw)?;
        let hash = HashKind::from_id(h.hash_kind_id)?;
        Self::validate_limits(
            h.bucket_capacity as usize,
            h.key_max(),
            h.val_max(),
            h.max_depth as u32,
        )?;

        let slots = 1usize << h.global_depth;
        let directory = read_directory(&mut pager, h.dir_start, slots)?;

        let mut idx = Index {
            pager,
            hash,
            capacity: h.capacity(),
            max_depth: h.max_depth as u32,
            key_max: h.key_max(),
            val_max: h.val_max(),
            global_depth: h.global_depth,
            bucket_count: h.bucket_count,
            free_head: h.free_head,
            high_water: h.high_water,
            dir_start: h.dir_start,
            dir_pages: h.dir_pages,
            directory,
            hook: None,
            recovered_intent: None,
        };
        if let Some(intent) = h.intent {
            idx.recover(intent)?;
        }
        Ok(idx)
    }

    pub fn set_hook(&mut self, hook: Arc<CrashHook>) {
        self.hook = Some(hook);
    }

    fn hook(&self, point: &str) {
        if let Some(h) = &self.hook {
            h(point);
        }
    }

    fn validate(cfg: &Config) -> Result<()> {
        if cfg.bucket_capacity < 1 || cfg.bucket_capacity > u16::MAX as usize {
            return Err(IndexError::Config("bucket_capacity must be in 1..=65535"));
        }
        Self::validate_limits(cfg.bucket_capacity, cfg.key_max, cfg.val_max, cfg.max_depth)
    }

    fn validate_limits(cap: usize, key_max: usize, val_max: usize, max_depth: u32) -> Result<()> {
        if !(1..=30).contains(&max_depth) {
            return Err(IndexError::Config("max_depth must be in 1..=30"));
        }
        if key_max == 0 || val_max == 0 {
            return Err(IndexError::Config("key_max/val_max must be positive sizes"));
        }
        let need = crate::store::bucket_required_size(cap, key_max, val_max);
        if need > PAGE_SIZE {
            return Err(IndexError::Config(
                "bucket pages cannot hold capacity entries of the given max key/value sizes; \
                 reduce --capacity or key/value limits",
            ));
        }
        Ok(())
    }

    // -- page allocation -------------------------------------------------
    //
    // Reservations are two-phase:
    // 1. `reserve_*` mutates the allocator state, after which the caller
    //    immediately writes a header (first the reservation header, then the
    //    intent header). A crash after a reservation header but before the
    //    intent leaves reserved pages unreachable (a bounded leak) — never a
    //    double allocation: pops from the free list and high-water bumps only
    //    become effective on disk through that header write.
    // 2. No allocator writes happen between the reservation header and the
    //    intent header, so an intent on disk always accounts for every page it
    //    names; redo needs no allocator changes.

    fn alloc_bucket_page(&mut self) -> u32 {
        if self.free_head != NULL_PAGE {
            let p = self.free_head;
            // Read the next-link first...
            let next = {
                let pg = self
                    .pager
                    .read_page(p)
                    .expect("free-list page must be readable");
                u32::from_le_bytes(pg[0..4].try_into().unwrap())
            };
            self.free_head = next;
            // ...then scrub the reused page: freed pages carry a link and a
            // stale checksum, and directory pages carry a checksum; a zeroed
            // page fails both decoders until the caller encodes real content.
            self.pager
                .write_page(p, &[0u8; PAGE_SIZE])
                .expect("scrubbing a reused page must succeed");
            p
        } else {
            let p = self.high_water;
            self.high_water += 1;
            p
        }
    }

    fn alloc_run(&mut self, n: u32) -> u32 {
        let start = self.high_water;
        self.high_water += n;
        start
    }

    /// Persist current allocator state without an intent — the reservation
    /// barrier. Any pages named by the intent written immediately afterwards
    /// are already guaranteed to exist and be unreachable from live state.
    fn reserve_header(&mut self) -> Result<()> {
        self.flush_header(None)
    }

    /// Append `pages` to the on-disk free list. Link pages are written here;
    /// they become reachable on the caller's following header write.
    fn push_free_list(&mut self, pages: &[u32]) -> Result<()> {
        let mut next = self.free_head;
        for &p in pages.iter().rev() {
            let mut pg = [0u8; PAGE_SIZE];
            pg[0..4].copy_from_slice(&next.to_le_bytes());
            self.pager.write_page(p, &pg)?;
            next = p;
        }
        if let Some(&first) = pages.first() {
            self.free_head = first;
        }
        Ok(())
    }

    fn flush_header(&mut self, intent: Option<Intent>) -> Result<()> {
        let header = Header {
            hash_kind_id: self.hash.id(),
            bucket_capacity: self.capacity as u16,
            global_depth: self.global_depth,
            bucket_count: self.bucket_count,
            free_head: self.free_head,
            high_water: self.high_water,
            max_depth: self.max_depth as u8,
            key_max: self.key_max as u32,
            val_max: self.val_max as u32,
            dir_start: self.dir_start,
            dir_pages: self.dir_pages,
            intent,
        };
        self.pager.write_header(&encode_header(&header))
    }

    fn read_bucket(&mut self, page: u32) -> Result<Bucket> {
        decode_bucket(&self.pager.read_page(page)?)
    }

    // -- public API ------------------------------------------------------

    /// Returns `true` if the key was newly inserted, `false` if the value of
    /// an existing key was overwritten.
    pub fn put(&mut self, key: &[u8], value: &[u8]) -> Result<bool> {
        if key.len() > self.key_max {
            return Err(IndexError::EntryTooLarge {
                what: "key",
                size: key.len(),
                limit: self.key_max,
            });
        }
        if value.len() > self.val_max {
            return Err(IndexError::EntryTooLarge {
                what: "value",
                size: value.len(),
                limit: self.val_max,
            });
        }
        let h = self.hash.hash(key);
        loop {
            let slot = (h as usize) & self.mask();
            let bucket_page = self.directory[slot];
            let mut bucket = self.read_bucket(bucket_page)?;

            if let Some(i) = bucket.entries.iter().position(|(k, _)| k.as_slice() == key) {
                bucket.entries[i].1 = value.to_vec();
                self.pager
                    .write_page(bucket_page, &encode_bucket(&bucket))?;
                self.pager.sync()?;
                return Ok(false);
            }

            if bucket.entries.len() < self.capacity {
                bucket.entries.push((key.to_vec(), value.to_vec()));
                self.pager
                    .write_page(bucket_page, &encode_bucket(&bucket))?;
                self.pager.sync()?;
                return Ok(true);
            }

            let ld = bucket.local_depth;
            if bucket.entries.iter().all(|(k, _)| self.hash.hash(k) == h) {
                return Err(IndexError::CollisionCapacity {
                    key: key.to_vec(),
                    hash: h,
                    bucket_capacity: self.capacity,
                });
            }
            if ld >= self.max_depth {
                return Err(IndexError::DepthLimit {
                    max_depth: self.max_depth,
                    bucket_capacity: self.capacity,
                });
            }
            self.split(&bucket, slot, bucket_page)?;
        }
    }

    pub fn get(&mut self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let h = self.hash.hash(key);
        let slot = (h as usize) & self.mask();
        let bucket_page = self.directory[slot];
        let bucket = self.read_bucket(bucket_page)?;
        Ok(bucket
            .entries
            .into_iter()
            .find(|(k, _)| k.as_slice() == key)
            .map(|(_, v)| v))
    }

    /// Returns `true` if the key existed and was removed.
    pub fn delete(&mut self, key: &[u8]) -> Result<bool> {
        let h = self.hash.hash(key);
        let slot = (h as usize) & self.mask();
        let bucket_page = self.directory[slot];
        let mut bucket = self.read_bucket(bucket_page)?;
        let before = bucket.entries.len();
        bucket.entries.retain(|(k, _)| k.as_slice() != key);
        if bucket.entries.len() == before {
            return Ok(false);
        }
        self.pager
            .write_page(bucket_page, &encode_bucket(&bucket))?;
        self.pager.sync()?;

        self.merge_after_delete(slot)?;
        self.shrink_if_possible()?;
        Ok(true)
    }

    pub fn stats(&mut self) -> Result<Stats> {
        let mut refs: BTreeMap<u32, usize> = BTreeMap::new();
        for &p in &self.directory {
            *refs.entry(p).or_default() += 1;
        }
        let mut buckets = Vec::new();
        for (page, slot_refs) in refs {
            let b = self.read_bucket(page)?;
            buckets.push(BucketStat {
                page,
                local_depth: b.local_depth,
                entries: b.entries.len(),
                slot_refs,
                keys: b
                    .entries
                    .iter()
                    .map(|(k, _)| String::from_utf8_lossy(k).into_owned())
                    .collect(),
            });
        }
        buckets.sort_by_key(|b| b.page);

        let mut free_pages = 0usize;
        let mut cur = self.free_head;
        while cur != NULL_PAGE {
            free_pages += 1;
            let pg = self.pager.read_page(cur)?;
            cur = u32::from_le_bytes(pg[0..4].try_into().unwrap());
        }
        let file_pages = self.pager.page_count()?;

        Ok(Stats {
            hash: self.hash.name().to_string(),
            global_depth: self.global_depth,
            directory_slots: self.directory.len(),
            directory_pages: self.dir_pages,
            bucket_count: self.bucket_count,
            free_pages,
            file_pages,
            bucket_capacity: self.capacity,
            max_depth: self.max_depth,
            key_max: self.key_max,
            val_max: self.val_max,
            recovered_intent: self.recovered_intent.clone(),
            buckets,
        })
    }

    pub fn recovered_intent(&self) -> Option<&str> {
        self.recovered_intent.as_deref()
    }

    // -- splitting -------------------------------------------------------

    fn mask(&self) -> usize {
        (1usize << self.global_depth) - 1
    }

    fn split(&mut self, old_bucket: &Bucket, slot: usize, old_page: u32) -> Result<()> {
        let ld = old_bucket.local_depth;
        let old_gd = self.global_depth;
        let doubled = ld == old_gd;
        let new_gd = old_gd + doubled as u32;
        let new_len = 1usize << new_gd;
        let old_dir_start = self.dir_start;
        let old_dir_pages = self.dir_pages;

        let new_a = self.alloc_bucket_page();
        let new_b = self.alloc_bucket_page();
        let new_dir_start = self.alloc_run(dir_pages_for(new_len));
        let new_dir_pages = dir_pages_for(new_len);

        let prefix_mask = (1usize << ld) - 1;
        let slot0 = (slot & prefix_mask) as u32;

        let intent = Intent::Split {
            slot0,
            old_bucket: old_page,
            new_a,
            new_b,
            new_dir_start,
            old_dir_start,
            old_global_depth: old_gd,
            local_depth: ld,
        };
        // Reservation barrier: pops/bumps durable before the intent exists.
        self.reserve_header()?;
        self.hook("split.after_reservation");
        // Intent barrier: reservations durable; live pages fully untouched.
        self.flush_header(Some(intent))?;
        self.hook("split.after_intent");

        let new_dir = self.build_split(
            old_bucket,
            old_gd,
            doubled,
            new_gd,
            slot0 as usize,
            new_dir_start,
            new_a,
            new_b,
            false,
        )?;

        self.commit_split(
            intent,
            new_dir,
            new_gd,
            new_dir_start,
            new_dir_pages,
            old_dir_start,
            old_dir_pages,
            old_page,
            None,
        )
    }

    /// Write the two result buckets and new directory, then commit. Used by
    /// both the normal split path and redo; callers guarantee
    /// `self.directory` is the *pre-move* run and `self.global_depth` is the
    /// pre-move depth.
    /// Build the result buckets and new directory from the untouched
    /// pre-split bucket/run. Returns nothing; the caller commits. Self is left
    /// with the new directory adopted (`self.directory` is the new run) but
    /// metadata/dir_start are unchanged — the commit helpers take it from
    /// there.
    #[allow(clippy::too_many_arguments)]
    fn build_split(
        &mut self,
        old_bucket: &Bucket,
        old_gd: u32,
        doubled: bool,
        new_gd: u32,
        slot0: usize,
        new_dir_start: u32,
        new_a: u32,
        new_b: u32,
        recovery: bool,
    ) -> Result<Vec<u32>> {
        let ld = old_bucket.local_depth;
        let bit = 1u64 << ld;
        let mut a_entries = Vec::new();
        let mut b_entries = Vec::new();
        for e in old_bucket.entries.iter() {
            if self.hash.hash(&e.0) & bit == 0 {
                a_entries.push(e.clone());
            } else {
                b_entries.push(e.clone());
            }
        }
        self.pager.write_page(
            new_a,
            &encode_bucket(&Bucket {
                local_depth: ld + 1,
                entries: a_entries,
            }),
        )?;
        self.pager.write_page(
            new_b,
            &encode_bucket(&Bucket {
                local_depth: ld + 1,
                entries: b_entries,
            }),
        )?;
        self.pager.sync()?;
        if !recovery {
            self.hook("split.after_buckets");
        }

        let old_len = 1usize << old_gd;
        let new_len = 1usize << new_gd;
        let pair_mask = (1usize << ld) - 1;
        let pair_bit = 1usize << ld;

        let mut new_dir = Vec::with_capacity(new_len);
        if doubled {
            // Directory doubled: old slot s is mirrored to s and s+old_len.
            for s in 0..new_len {
                let src = s & (old_len - 1);
                if src & pair_mask == slot0 {
                    new_dir.push(if s & pair_bit == 0 { new_a } else { new_b });
                } else {
                    new_dir.push(self.directory[src]);
                }
            }
        } else {
            // Shared-bucket split: only the pair's slots move.
            for s in 0..new_len {
                if s & pair_mask == slot0 {
                    new_dir.push(if s & pair_bit == 0 { new_a } else { new_b });
                } else {
                    new_dir.push(self.directory[s]);
                }
            }
        }
        self.pager
            .write_run(new_dir_start, &encode_directory(&new_dir))?;
        self.pager.sync()?;
        if !recovery {
            self.hook("split.after_directory");
        }
        Ok(new_dir)
    }

    /// Commit a built split.
    ///
    /// * normal path: two-phase — adopt metadata with intent still live
    ///   (hook `after_commit_meta`), then free dead pages and clear intent.
    /// * recovery path (`committed_meta` records what the crashed header
    ///   showed): rebuild already happened from pre-move pages; adopt
    ///   metadata unconditionally (setting rather than incrementing bucket
    ///   count/global depth) and free the dead pages once. A header already
    ///   past phase 1 simply gets the same final state rewritten.
    #[allow(clippy::too_many_arguments)]
    fn commit_split(
        &mut self,
        intent: Intent,
        new_dir: Vec<u32>,
        new_gd: u32,
        new_dir_start: u32,
        new_dir_pages: u32,
        old_dir_start: u32,
        old_dir_pages: u32,
        old_page: u32,
        recovery: Option<bool>, // Some(meta_already_committed) when replaying
    ) -> Result<()> {
        self.directory = new_dir;
        self.dir_start = new_dir_start;
        self.dir_pages = new_dir_pages;
        match recovery {
            None => {
                self.global_depth = new_gd;
                self.bucket_count += 1;
                self.flush_header(Some(intent))?;
                self.hook("split.after_commit_meta");
            }
            Some(meta_committed) => {
                // Redo: the crashed header may or may not carry post-move
                // metadata; force the correct values either way.
                self.global_depth = new_gd;
                if !meta_committed {
                    self.bucket_count += 1;
                }
            }
        }

        // Only bucket pages return to the free list. Old directory runs are
        // NOT freed: a stale directory slot may still name a page inside a
        // superseded run (it is harmless until overwritten, but reusing that
        // page as a bucket would make those stale slots point at live data).
        // Directory runs are bump-only and their total size is bounded by
        // 2^(max_depth+1) slots.
        let _ = (old_dir_start, old_dir_pages);
        let dead = vec![old_page];
        self.push_free_list(&dead)?;
        self.flush_header(None)?;
        if recovery.is_none() {
            self.hook("split.after_commit_free");
        }
        Ok(())
    }

    // -- merging ----------------------------------------------------------

    /// After deleting from the bucket referenced by `slot`, repeatedly merge
    /// equal-depth buddy pairs whose entries fit in a single bucket.
    ///
    /// Low-bit layout: a depth-`ld` bucket's slots are the progression
    /// `prefix, prefix+2^ld, prefix+2*2^ld, …` (each slot's low `ld` bits are
    /// the bucket's prefix). Its equal-depth buddy flips bit `ld-1`. Two
    /// distinct pages of equal local depth merge iff their combined entries
    /// fit one bucket; the merged bucket (depth `ld-1`) then owns the
    /// progression with stride `2^(ld-1)` — see `build_merge`.
    fn merge_after_delete(&mut self, mut slot: usize) -> Result<()> {
        loop {
            let page_a = self.directory[slot];
            let ba = self.read_bucket(page_a)?;
            let ld = ba.local_depth;
            if ld == 0 {
                return Ok(());
            }
            let buddy_bit = 1usize << (ld - 1);
            let slot0 = slot & !buddy_bit;
            let slot_b = slot0 | buddy_bit;
            let page_b = self.directory[slot_b];
            if page_b == page_a {
                // Buddy slots alias a shallower shared bucket; wait for a
                // delete that lands in that shallower bucket.
                return Ok(());
            }
            let bb = self.read_bucket(page_b)?;
            if bb.local_depth != ld {
                return Ok(());
            }
            if ba.entries.len() + bb.entries.len() > self.capacity {
                return Ok(());
            }
            self.merge_pair(slot0, page_a, &ba, page_b, &bb, ld)?;
            slot = slot0; // continue cascading at the merged depth
        }
    }

    fn merge_pair(
        &mut self,
        slot0: usize,
        page_a: u32,
        ba: &Bucket,
        page_b: u32,
        bb: &Bucket,
        ld: u32,
    ) -> Result<()> {
        let len = 1usize << self.global_depth;
        let old_dir_start = self.dir_start;
        let old_dir_pages = self.dir_pages;
        let merged_page = self.alloc_bucket_page();
        let new_dir_start = self.alloc_run(dir_pages_for(len));
        let new_dir_pages = dir_pages_for(len);

        let intent = Intent::Merge {
            slot0: slot0 as u32,
            bucket_a: page_a,
            bucket_b: page_b,
            merged_page,
            new_dir_start,
            old_dir_start,
        };
        self.reserve_header()?;
        self.hook("merge.after_reservation");
        self.flush_header(Some(intent))?;
        self.hook("merge.after_intent");

        let new_dir = self.build_merge(
            slot0,
            page_a,
            page_b,
            ba,
            bb,
            ld,
            merged_page,
            new_dir_start,
            false,
        )?;
        self.commit_merge(
            intent,
            new_dir,
            page_a,
            page_b,
            old_dir_start,
            old_dir_pages,
            new_dir_start,
            new_dir_pages,
            None,
        )
    }

    /// Build merged bucket page + new directory run from the two untouched
    /// pre-merge buckets; returns the new directory vector.
    #[allow(clippy::too_many_arguments)]
    fn build_merge(
        &mut self,
        slot0: usize,
        page_a: u32,
        page_b: u32,
        ba: &Bucket,
        bb: &Bucket,
        ld: u32,
        merged_page: u32,
        new_dir_start: u32,
        recovery: bool,
    ) -> Result<Vec<u32>> {
        let len = self.directory.len();
        let mut entries = ba.entries.clone();
        entries.extend(bb.entries.clone());
        self.pager.write_page(
            merged_page,
            &encode_bucket(&Bucket {
                local_depth: ld - 1,
                entries,
            }),
        )?;
        self.pager.sync()?;
        if !recovery {
            self.hook("merge.after_bucket");
        }

        // LOW-BIT extendible hashing merge mapping. Two depth-`ld` buddies
        // (prefixes differ in bit `ld-1`, same below) collapse into one bucket
        // of depth `ld-1` whose slots are all `s` with
        //     s mod 2^(ld-1) == slot0
        // i.e. an arithmetic progression with stride 2^(ld-1). Only slots
        // currently pointing at page_a/page_b move; shallower aliases (same
        // page number) move correctly with them.
        let new_ld = ld - 1;
        let mut new_dir = self.directory.clone();
        let mut repointed = 0usize;
        if new_ld == 0 {
            // Merging into a depth-0 bucket. This is reachable only when the
            // pair covers every directory slot (any deeper buddy would have a
            // different local depth and blocked the merge), but repoint by
            // page number anyway to make an impossible stray slot harmless.
            for p in new_dir.iter_mut() {
                if *p == page_a || *p == page_b {
                    *p = merged_page;
                    repointed += 1;
                }
            }
        } else {
            let stride = 1usize << new_ld;
            let prefix = slot0 % stride;
            let mut s = prefix;
            while s < len {
                let p = self.directory[s];
                if p == page_a || p == page_b {
                    new_dir[s] = merged_page;
                    repointed += 1;
                }
                s += stride;
            }
        }
        debug_assert!(repointed >= 2, "merge must repoint at least a pair");
        self.pager
            .write_run(new_dir_start, &encode_directory(&new_dir))?;
        self.pager.sync()?;
        if !recovery {
            self.hook("merge.after_directory");
        }
        Ok(new_dir)
    }

    /// Commit a built merge. `recovery = Some(meta_committed)` during redo;
    /// see [`Index::commit_split`] for the two-phase rationale.
    #[allow(clippy::too_many_arguments)]
    fn commit_merge(
        &mut self,
        intent: Intent,
        new_dir: Vec<u32>,
        page_a: u32,
        page_b: u32,
        old_dir_start: u32,
        old_dir_pages: u32,
        new_dir_start: u32,
        new_dir_pages: u32,
        recovery: Option<bool>,
    ) -> Result<()> {
        self.directory = new_dir;
        self.dir_start = new_dir_start;
        self.dir_pages = new_dir_pages;
        match recovery {
            None => {
                self.bucket_count -= 1;
                self.flush_header(Some(intent))?;
                self.hook("merge.after_commit_meta");
            }
            Some(meta_committed) => {
                if !meta_committed {
                    self.bucket_count -= 1;
                }
            }
        }

        // Free only the two old bucket pages; superseded directory runs are
        // never returned to the free list (see commit_split).
        let _ = (old_dir_start, old_dir_pages);
        let dead = vec![page_a, page_b];
        self.push_free_list(&dead)?;
        self.flush_header(None)?;
        if recovery.is_none() {
            self.hook("merge.after_commit_free");
        }
        Ok(())
    }

    // -- directory shrink -------------------------------------------------

    fn shrink_if_possible(&mut self) -> Result<()> {
        while self.global_depth > 0 {
            let half = 1usize << (self.global_depth - 1);
            if self.directory[..half] != self.directory[half..] {
                return Ok(());
            }
            self.shrink()?;
        }
        Ok(())
    }

    fn shrink(&mut self) -> Result<()> {
        // The live halves are identical, so no page moves are needed: the
        // intent header makes the flip atomic in one extra durable write.
        self.flush_header(Some(Intent::Shrink))?;
        self.hook("shrink.after_intent");
        let half = 1usize << (self.global_depth - 1);
        self.directory.truncate(half);
        self.global_depth -= 1;
        self.flush_header(None)?;
        self.hook("shrink.after_commit");
        Ok(())
    }

    // -- recovery ---------------------------------------------------------
    //
    // A live intent can be found in one of two crash windows:
    //
    // * pre-commit-meta: the header still carries pre-move metadata (global
    //   depth, bucket count, dir_start); new result pages may be partially
    //   written or absent.
    // * post-commit-meta: the header carries post-move metadata and already
    //   points at the new run, but the intent is still set and the dead pages
    //   are not yet on the free list (the crash hit phase 2).
    //
    // Redo handles both the same way: detect which window from the header,
    // normalize self back to the *pre-move* state, rebuild every new page
    // deterministically from the untouched pre-move pages (partial new pages
    // are never read), then write the single final committed header.

    fn recover(&mut self, intent: Intent) -> Result<()> {
        let tag = match intent {
            Intent::Split { .. } => "split",
            Intent::Merge { .. } => "merge",
            Intent::Shrink => "shrink",
        };
        match intent {
            Intent::Split {
                slot0,
                old_bucket,
                new_a,
                new_b,
                new_dir_start,
                old_dir_start,
                old_global_depth,
                local_depth,
            } => self.recover_split(
                slot0,
                old_bucket,
                new_a,
                new_b,
                new_dir_start,
                old_dir_start,
                old_global_depth,
                local_depth,
            )?,
            Intent::Merge {
                slot0,
                bucket_a,
                bucket_b,
                merged_page,
                new_dir_start,
                old_dir_start,
            } => self.recover_merge(
                slot0,
                bucket_a,
                bucket_b,
                merged_page,
                new_dir_start,
                old_dir_start,
            )?,
            Intent::Shrink => {
                // With the intent still live the depth flip never committed,
                // so apply it now. Directory halves are physically identical,
                // therefore no page moves are required.
                let half = 1usize << (self.global_depth - 1);
                self.directory.truncate(half);
                self.global_depth -= 1;
                self.flush_header(None)?;
            }
        }
        self.recovered_intent = Some(tag.to_string());
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn recover_split(
        &mut self,
        slot0: u32,
        old_bucket: u32,
        new_a: u32,
        new_b: u32,
        new_dir_start: u32,
        old_dir_start: u32,
        old_gd: u32,
        ld: u32,
    ) -> Result<()> {
        let doubled = ld == old_gd;
        let new_gd = old_gd + doubled as u32;
        let old_dir_pages = dir_pages_for(1usize << old_gd);
        let new_dir_pages = dir_pages_for(1usize << new_gd);

        // Phase-1 detection: after commit the run start moved (and, for a
        // doubled split, global depth advanced).
        let meta_committed = self.dir_start == new_dir_start;

        // Reload the pre-move run (always intact) and normalize metadata.
        let old_slots = read_directory(&mut self.pager, old_dir_start, 1usize << old_gd)?;
        self.directory = old_slots;
        let old = self.read_bucket(old_bucket)?;
        if meta_committed {
            // Phase 1 already moved global depth; bucket count is correct for
            // the post-split state and must not be touched.
            self.global_depth = old_gd;
        }
        self.dir_start = old_dir_start;
        self.dir_pages = old_dir_pages;

        let new_dir = self.build_split(
            &old,
            old_gd,
            doubled,
            new_gd,
            slot0 as usize,
            new_dir_start,
            new_a,
            new_b,
            true,
        )?;

        self.commit_split(
            Intent::Split {
                slot0,
                old_bucket,
                new_a,
                new_b,
                new_dir_start,
                old_dir_start,
                old_global_depth: old_gd,
                local_depth: ld,
            },
            new_dir,
            new_gd,
            new_dir_start,
            new_dir_pages,
            old_dir_start,
            old_dir_pages,
            old_bucket,
            Some(meta_committed),
        )
    }

    fn recover_merge(
        &mut self,
        slot0: u32,
        bucket_a: u32,
        bucket_b: u32,
        merged_page: u32,
        new_dir_start: u32,
        old_dir_start: u32,
    ) -> Result<()> {
        let meta_committed = self.dir_start == new_dir_start;
        let gd = self.global_depth;
        let dir_pages = dir_pages_for(1usize << gd);

        let ba = self.read_bucket(bucket_a)?;
        let bb = self.read_bucket(bucket_b)?;
        let ld = ba.local_depth;
        if bb.local_depth != ld {
            return Err(IndexError::Corrupt(format!(
                "merge intent buckets have mismatched local depths {} vs {}",
                ld, bb.local_depth
            )));
        }

        // Normalize to the pre-move run/metadata. Bucket count is already
        // correct for whichever window crashed (phase 1 decremented it), and
        // commit_merge in recovery mode leaves it untouched.
        let old_slots = read_directory(&mut self.pager, old_dir_start, 1usize << gd)?;
        self.directory = old_slots;
        self.dir_start = old_dir_start;
        self.dir_pages = dir_pages;

        let new_dir = self.build_merge(
            slot0 as usize,
            bucket_a,
            bucket_b,
            &ba,
            &bb,
            ld,
            merged_page,
            new_dir_start,
            true,
        )?;

        self.commit_merge(
            Intent::Merge {
                slot0,
                bucket_a,
                bucket_b,
                merged_page,
                new_dir_start,
                old_dir_start,
            },
            new_dir,
            bucket_a,
            bucket_b,
            old_dir_start,
            dir_pages,
            new_dir_start,
            dir_pages,
            Some(meta_committed),
        )
    }
}
