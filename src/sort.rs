//! External sort engine: map (spill sorted segments), then multi-pass k-way
//! merge — all resumable from the committed manifest.
//!
//! ## Map phase
//!
//! Input is scanned with a bounded [`LineScanner`]. Resident records
//! accumulate until their bytes reach the budgeted record allowance; they are
//! then stable-sorted in memory (composite key, seq tie-break) and spilled as
//! one committed level-0 run. A record too large to be resident streams
//! straight into its own single-record run. After every run commit the
//! manifest is atomically fsync-updated. On resume the input is reopened and
//! the already-consumed records are skipped.
//!
//! ## Merge phase
//!
//! Unmerged runs for the active level are held in `manifest.runs`; committed
//! batch outputs of the in-progress pass are in `manifest.merge_done`
//! (one level up). Batches are formed greedily (`lanes` inputs each); after a
//! batch's output is committed its input runs are removed from the manifest
//! and their files deleted. A crash therefore redoes at most the one
//! uncommitted batch — earlier batches are reused from disk. When a pass
//! produces exactly one run it is streamed to `output.txt`; zero records yield
//! an empty output.
//!
//! ## Memory
//!
//! Merge lanes stream values; only the bounded head chunk is resident per
//! lane. Fixed buffers (input read buffer, output buffer, spill work area,
//! per-lane readahead + head) are reserved by [`crate::budget::Budget`]; the
//! record allowance is what remains.

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::io::Read;
use std::sync::Arc;

use crate::budget::Budget;
use crate::error::{Error, Result};
use crate::io::{VFile, Vfs};
use crate::key::{compare_records, KeySpec};
use crate::repository::{Job, JobState, Repository, RunEntry};
use crate::scanner::{LineScanner, ScannerEvent};
use crate::sortfile::{GiantDrain, Pull, RecordHead, RunReader, RunWriter};

/// Counters produced by a (possibly resumed) run.
#[derive(Debug, Clone, Default)]
pub struct SortStats {
    pub records: u64,
    pub level0_runs: usize,
    pub merge_passes: u32,
    pub merge_batches: usize,
    /// True when this invocation resumed previously committed work.
    pub resumed: bool,
    pub output_bytes: u64,
}

/// One resident record awaiting an in-memory sort.
struct Resident {
    line: Vec<u8>,
    keys: Vec<Vec<u8>>,
    seq: u64,
}

/// Run a job to completion, resuming from whatever the manifest committed.
pub fn run_job(repo: &Repository, job: &mut Job) -> Result<SortStats> {
    let spec = KeySpec::parse(&job.cfg.key_spec, job.cfg.delim)?;
    if job.cfg.max_lanes < 2 {
        return Err(Error::Config("max_lanes must be at least 2".into()));
    }
    let budget = Budget::plan(job.cfg.budget, job.cfg.max_lanes)?;
    let resumed = job.manifest.state == JobState::Merge
        || (job.manifest.state == JobState::Map && job.manifest.map_consumed > 0);
    let mut stats = SortStats {
        resumed,
        ..SortStats::default()
    };

    if job.manifest.state == JobState::Map {
        map_phase(repo, job, &spec, &budget, &mut stats)?;
    }
    if job.manifest.state == JobState::Merge {
        merge_phase(repo, job, &spec, &budget, &mut stats)?;
    }
    if job.manifest.state == JobState::Done && !job.cfg.keep_temp {
        repo.cleanup_levels(job)?;
    }
    stats.records = job.manifest.total_records;
    Ok(stats)
}

// ===========================================================================
// Map
// ===========================================================================

fn map_phase(
    repo: &Repository,
    job: &mut Job,
    spec: &KeySpec,
    budget: &Budget,
    stats: &mut SortStats,
) -> Result<()> {
    let vfs = repo.vfs();

    // A resident line is bounded by both the record allowance and a run head
    // chunk (so a small record always serializes in one FIRST chunk).
    let cap = budget.record_bytes().min(crate::sortfile::CHUNK_MAX - 64);
    if cap < 64 {
        return Err(Error::BudgetTooSmall {
            budget: budget.total,
            min: crate::budget::MIN_BUDGET,
        });
    }
    // The giant head window must leave room for the key framing overhead in a
    // FIRST chunk; reserve a fixed margin.
    let head_limit = crate::sortfile::CHUNK_MAX - 128;

    // Re-open input; skip records already persisted by earlier attempts.
    let input = vfs.open(&job.input_path())?;
    let mut scanner = LineScanner::new(vfile_reader(input), spec.clone(), cap, head_limit);
    let mut skipped: u64 = 0;
    let skip_to = job.manifest.map_consumed;
    while skipped < skip_to {
        match scanner.next()? {
            ScannerEvent::End => {
                return Err(Error::InconsistentState(format!(
                    "resume expected {skip_to} prior records, input ended at {skipped}"
                )));
            }
            ScannerEvent::Small(_) => skipped += 1,
            ScannerEvent::Giant { .. } => {
                drain_giant(&mut scanner)?;
                skipped += 1;
            }
        }
    }

    let mut residents: Vec<Resident> = Vec::new();
    let mut resident_bytes = 0usize;
    let allow = budget.record_bytes();
    let mut next_run = job.manifest.runs.len();

    loop {
        match scanner.next()? {
            ScannerEvent::End => break,
            ScannerEvent::Small(rec) => {
                let keys = extract_keys(spec, &rec.line);
                resident_bytes += rec.line.len() + keys.iter().map(|k| k.len()).sum::<usize>();
                residents.push(Resident {
                    line: rec.line,
                    keys,
                    seq: rec.seq,
                });
                if resident_bytes >= allow {
                    spill_residents(repo, job, spec, &mut residents, &mut next_run)?;
                    resident_bytes = 0;
                }
            }
            ScannerEvent::Giant { seq, keys, prefix } => {
                // Flush buffered residents first, then stream the giant into a
                // dedicated single-record run (never co-resident).
                if !residents.is_empty() {
                    spill_residents(repo, job, spec, &mut residents, &mut next_run)?;
                    resident_bytes = 0;
                }
                let name = format!("run-{next_run:05}");
                next_run += 1;
                let dir = job.level_dir(0);
                let count = {
                    let mut w = RunWriter::create(vfs.as_ref(), &dir, &name)?;
                    w.write_giant(seq, &keys, &prefix, &mut scanner)?;
                    let (_, n) = w.commit(vfs.as_ref())?;
                    n
                };
                job.manifest.map_consumed += 1;
                job.manifest.total_records += 1;
                job.manifest.runs.push(RunEntry {
                    name: format!("{name}.run"),
                    records: count,
                });
                repo.commit_with_status(job, "map:giant-run")?;
            }
        }
    }

    if !residents.is_empty() {
        spill_residents(repo, job, spec, &mut residents, &mut next_run)?;
    }

    stats.level0_runs = job.manifest.runs.len();
    job.manifest.state = JobState::Merge;
    job.manifest.level = 0;
    job.manifest.merge_done.clear();
    repo.commit_with_status(job, "merge:begin")?;
    Ok(())
}

fn extract_keys(spec: &KeySpec, line: &[u8]) -> Vec<Vec<u8>> {
    spec.extract(line).into_iter().map(|k| k.to_vec()).collect()
}

/// Stable-sort buffered residents and spill one level-0 run; manifest commit.
fn spill_residents(
    repo: &Repository,
    job: &mut Job,
    spec: &KeySpec,
    residents: &mut Vec<Resident>,
    next_run: &mut usize,
) -> Result<()> {
    if residents.is_empty() {
        return Ok(());
    }
    residents.sort_by(|a, b| {
        let ak: Vec<&[u8]> = a.keys.iter().map(|k| k.as_slice()).collect();
        let bk: Vec<&[u8]> = b.keys.iter().map(|k| k.as_slice()).collect();
        compare_records(spec, &ak, a.seq, &bk, b.seq)
    });

    let name = format!("run-{:05}", *next_run);
    *next_run += 1;
    let dir = job.level_dir(0);
    let vfs = repo.vfs();
    {
        let mut w = RunWriter::create(vfs.as_ref(), &dir, &name)?;
        for r in residents.iter() {
            w.write_record(r.seq, &r.keys, &r.line)?;
        }
        let (_, committed) = w.commit(vfs.as_ref())?;
        assert_eq!(committed, residents.len() as u64);
    }

    let spilled = residents.len() as u64;
    residents.clear();

    job.manifest.map_consumed += spilled;
    job.manifest.total_records += spilled;
    job.manifest.runs.push(RunEntry {
        name: format!("{name}.run"),
        records: spilled,
    });
    repo.commit_with_status(job, "map:run")?;
    Ok(())
}

// ===========================================================================
// Merge
// ===========================================================================

fn merge_phase(
    repo: &Repository,
    job: &mut Job,
    spec: &KeySpec,
    budget: &Budget,
    stats: &mut SortStats,
) -> Result<()> {
    let vfs = repo.vfs();
    let lanes = budget.lanes().min(job.cfg.max_lanes);

    loop {
        // Empty input -> empty output.
        if job.manifest.runs.is_empty() && job.manifest.merge_done.is_empty() {
            write_empty_output(repo, job, stats)?;
            return Ok(());
        }
        // Exactly one run remains -> it is the sorted result.
        if job.manifest.merge_done.is_empty() && job.manifest.runs.len() == 1 {
            stream_single_run(repo, job, budget, stats)?;
            return Ok(());
        }

        // A pass whose full input fits in `lanes` is the final merge and goes
        // straight to output. merge_done>0 means a non-final pass is already
        // underway (a final pass never commits batch outputs).
        let pass_is_final = job.manifest.merge_done.is_empty() && job.manifest.runs.len() <= lanes;

        stats.merge_passes += 1;
        let level = job.manifest.level;
        let next_level = level + 1;

        if pass_is_final {
            let group: Vec<RunEntry> = std::mem::take(&mut job.manifest.runs);
            merge_final_to_output(repo, job, level, &group, spec, budget, stats)?;
            return Ok(());
        }

        // Non-final pass: merge batches to level+1 runs. Input runs are kept
        // on disk until the entire pass rotates, so a crash after some batches
        // still has every input needed to redo the unfinished batch.
        vfs.make_dir_all(&job.level_dir(next_level))?;
        let mut batch = job.manifest.merge_done.len();
        while !job.manifest.runs.is_empty() {
            let take = lanes.min(job.manifest.runs.len());
            // Borrow without mutating first: if the merge fails the manifest
            // still lists these inputs for resume.
            let group: Vec<RunEntry> = job.manifest.runs[..take].to_vec();
            let out_name = format!("m-{next_level}-{batch:05}");
            let count = merge_batch_to_run(repo, job, next_level, &group, &out_name, spec, budget)?;
            // Only after the output run is durable: consume inputs, record it.
            job.manifest.runs.drain(..take);
            stats.merge_batches += 1;
            batch += 1;
            job.manifest.merge_done.push(RunEntry {
                name: format!("{out_name}.run"),
                records: count,
            });
            repo.commit_with_status(job, "merge:batch")?;
        }

        // Rotate to the next level. Only NOW, after all outputs are committed,
        // reclaim the consumed input level (unless the user asked to retain
        // every intermediate level for inspection).
        job.manifest.runs = std::mem::take(&mut job.manifest.merge_done);
        job.manifest.level = next_level;
        repo.commit_with_status(job, "merge:level")?;
        if !job.cfg.keep_temp {
            repo.delete_level_runs(job, level)?;
        }
    }
}

/// Where a k-way merge writes its records.
trait MergeSink {
    fn emit_record(&mut self, seq: u64, keys: &[Vec<u8>], value: &[u8]) -> Result<()>;
    fn emit_giant(
        &mut self,
        seq: u64,
        keys: &[Vec<u8>],
        prefix: &[u8],
        drain: &mut dyn GiantDrain,
    ) -> Result<()>;
}

struct RunSink<'a> {
    writer: &'a mut RunWriter,
}
impl<'a> MergeSink for RunSink<'a> {
    fn emit_record(&mut self, seq: u64, keys: &[Vec<u8>], value: &[u8]) -> Result<()> {
        self.writer.write_record(seq, keys, value)
    }
    fn emit_giant(
        &mut self,
        seq: u64,
        keys: &[Vec<u8>],
        prefix: &[u8],
        drain: &mut dyn GiantDrain,
    ) -> Result<()> {
        self.writer.write_giant(seq, keys, prefix, drain)
    }
}

struct OutputSink<'a> {
    out: &'a mut Box<dyn VFile>,
    bytes: u64,
}
impl<'a> MergeSink for OutputSink<'a> {
    fn emit_record(&mut self, _seq: u64, _keys: &[Vec<u8>], value: &[u8]) -> Result<()> {
        self.out.write_all(value)?;
        self.out.write_all(b"\n")?;
        self.bytes += value.len() as u64 + 1;
        Ok(())
    }
    fn emit_giant(
        &mut self,
        _seq: u64,
        _keys: &[Vec<u8>],
        prefix: &[u8],
        drain: &mut dyn GiantDrain,
    ) -> Result<()> {
        self.out.write_all(prefix)?;
        self.bytes += prefix.len() as u64;
        let mut buf = vec![0u8; crate::budget::MERGE_LANE_BUFFER];
        loop {
            let p = drain.pull(&mut buf)?;
            if p.bytes > 0 {
                self.out.write_all(&buf[..p.bytes])?;
                self.bytes += p.bytes as u64;
            }
            if p.end_of_record {
                break;
            }
        }
        self.out.write_all(b"\n")?;
        self.bytes += 1;
        Ok(())
    }
}

/// One k-way merge input.
struct Lane {
    reader: RunReader,
    head: RecordHead,
}

struct HeapEntry {
    keys: Vec<Vec<u8>>,
    seq: u64,
    lane: usize,
}

/// BinaryHeap entry ordered so the globally smallest `(key, seq)` pops first
/// (BinaryHeap is a max-heap, hence the reversed comparator).
struct CmpItem<'a> {
    inner: HeapEntry,
    spec: &'a KeySpec,
}
impl<'a> PartialEq for CmpItem<'a> {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl<'a> Eq for CmpItem<'a> {}
impl<'a> PartialOrd for CmpItem<'a> {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl<'a> Ord for CmpItem<'a> {
    fn cmp(&self, o: &Self) -> Ordering {
        let ak: Vec<&[u8]> = self.inner.keys.iter().map(|k| k.as_slice()).collect();
        let bk: Vec<&[u8]> = o.inner.keys.iter().map(|k| k.as_slice()).collect();
        compare_records(self.spec, &ak, self.inner.seq, &bk, o.inner.seq).reverse()
    }
}

/// Core k-way merge: pull heads from `lanes`, emit through `sink` in stable
/// `(key, seq)` order. `io_buf` is the bounded buffer used to stream giant
/// values.
#[allow(clippy::too_many_arguments)]
fn kway_merge(
    vfs: &Arc<dyn Vfs>,
    job: &Job,
    level: u32,
    group: &[RunEntry],
    spec: &KeySpec,
    io_buf: &mut [u8],
    sink: &mut dyn MergeSink,
) -> Result<u64> {
    let mut lanes: Vec<Option<Lane>> = Vec::with_capacity(group.len());
    for r in group {
        let path = job.run_path(level, &r.name);
        let mut reader = RunReader::open(vfs.as_ref(), &path)?;
        let head = reader
            .next_head()?
            .ok_or_else(|| Error::corrupt(&path, "merge input unexpectedly empty"))?;
        lanes.push(Some(Lane { reader, head }));
    }

    let mut heap: BinaryHeap<CmpItem> = BinaryHeap::with_capacity(lanes.len());
    for (li, slot) in lanes.iter().enumerate() {
        let l = slot.as_ref().unwrap();
        heap.push(CmpItem {
            inner: HeapEntry {
                keys: l.head.keys.clone(),
                seq: l.head.seq,
                lane: li,
            },
            spec,
        });
    }

    let mut emitted: u64 = 0;
    while let Some(top) = heap.pop() {
        let li = top.inner.lane;
        let mut lane = lanes[li].take().expect("lane present");

        // Emit the lane's current record.
        let seq = lane.head.seq;
        let keys = std::mem::take(&mut lane.head.keys);
        let has_more = lane.head.has_more;
        if !has_more {
            let value = std::mem::take(&mut lane.head.prefix);
            sink.emit_record(seq, &keys, &value)?;
        } else {
            let prefix = std::mem::take(&mut lane.head.prefix);
            let mut drain = LaneDrain {
                reader: &mut lane.reader,
                buf: io_buf,
            };
            sink.emit_giant(seq, &keys, &prefix, &mut drain)?;
        }
        emitted += 1;

        // Advance the lane.
        match lane.reader.next_head()? {
            Some(h) => {
                let k = h.keys.clone();
                let s = h.seq;
                lane.head = h;
                lanes[li] = Some(lane);
                heap.push(CmpItem {
                    inner: HeapEntry {
                        keys: k,
                        seq: s,
                        lane: li,
                    },
                    spec,
                });
            }
            None => {
                lane.reader.finish()?; // verify trailer/CRCs of drained run
                lanes[li] = None;
            }
        }
    }
    Ok(emitted)
}

/// Streams continuation bytes of one merge lane into a giant-record writer.
struct LaneDrain<'a> {
    reader: &'a mut RunReader,
    buf: &'a mut [u8],
}
impl<'a> GiantDrain for LaneDrain<'a> {
    fn pull(&mut self, out: &mut [u8]) -> Result<Pull> {
        let want = out.len().min(self.buf.len());
        let n = self.reader.read_value(&mut out[..want])?;
        Ok(Pull {
            bytes: n,
            end_of_record: n == 0,
        })
    }
}

/// Merge one batch into a committed run at `next_level` (its inputs live at
/// `next_level - 1`).
fn merge_batch_to_run(
    repo: &Repository,
    job: &Job,
    next_level: u32,
    group: &[RunEntry],
    out_name: &str,
    spec: &KeySpec,
    budget: &Budget,
) -> Result<u64> {
    let vfs = repo.vfs();
    let dir = job.level_dir(next_level);
    let mut writer = RunWriter::create(vfs.as_ref(), &dir, out_name)?;
    let mut io_buf = vec![0u8; budget.lane_buffer];
    let count = {
        let mut sink = RunSink {
            writer: &mut writer,
        };
        kway_merge(
            &vfs,
            job,
            next_level - 1,
            group,
            spec,
            &mut io_buf,
            &mut sink,
        )?
    };
    let (_, count_committed) = writer.commit(vfs.as_ref())?;
    assert_eq!(count, count_committed);
    Ok(count_committed)
}

/// Final merge: k-way merge straight into `output.txt.tmp`, fsync, rename.
fn merge_final_to_output(
    repo: &Repository,
    job: &mut Job,
    level: u32,
    group: &[RunEntry],
    spec: &KeySpec,
    budget: &Budget,
    stats: &mut SortStats,
) -> Result<()> {
    let vfs = repo.vfs();
    let out_tmp = format!("jobs/{}/output.txt.tmp", job.id);
    let out_final = job.output_path();
    let written;
    {
        let mut out = vfs.create(&out_tmp)?;
        let mut io_buf = vec![0u8; budget.lane_buffer];
        let mut sink = OutputSink {
            out: &mut out,
            bytes: 0,
        };
        let count = kway_merge(&vfs, job, level, group, spec, &mut io_buf, &mut sink)?;
        debug_assert_eq!(count, job.manifest.total_records);
        written = sink.bytes;
        out.flush()?;
        out.sync_all()?;
    }
    vfs.rename(&out_tmp, &out_final)?;

    stats.merge_batches += 1;
    finish_done(repo, job, stats, written)?;
    Ok(())
}

/// Stream a single already-sorted run to the output file.
fn stream_single_run(
    repo: &Repository,
    job: &mut Job,
    budget: &Budget,
    stats: &mut SortStats,
) -> Result<()> {
    let vfs = repo.vfs();
    let level = job.manifest.level;
    let entry = job.manifest.runs[0].clone();
    let out_tmp = format!("jobs/{}/output.txt.tmp", job.id);
    let out_final = job.output_path();
    let mut bytes = 0u64;
    {
        let mut out = vfs.create(&out_tmp)?;
        let path = job.run_path(level, &entry.name);
        let mut reader = RunReader::open(vfs.as_ref(), &path)?;
        let mut buf = vec![0u8; budget.lane_buffer];
        while let Some(head) = reader.next_head()? {
            out.write_all(&head.prefix)?;
            bytes += head.prefix.len() as u64;
            if head.has_more {
                loop {
                    let n = reader.read_value(&mut buf)?;
                    if n == 0 {
                        break;
                    }
                    out.write_all(&buf[..n])?;
                    bytes += n as u64;
                }
            }
            out.write_all(b"\n")?;
            bytes += 1;
        }
        reader.finish()?;
        out.flush()?;
        out.sync_all()?;
    }
    vfs.rename(&out_tmp, &out_final)?;
    finish_done(repo, job, stats, bytes)?;
    Ok(())
}

fn write_empty_output(repo: &Repository, job: &mut Job, stats: &mut SortStats) -> Result<()> {
    let vfs = repo.vfs();
    let tmp = format!("jobs/{}/output.txt.tmp", job.id);
    let final_path = job.output_path();
    {
        let f = vfs.create(&tmp)?;
        f.sync_all()?;
    }
    vfs.rename(&tmp, &final_path)?;
    finish_done(repo, job, stats, 0)?;
    Ok(())
}

fn finish_done(
    repo: &Repository,
    job: &mut Job,
    stats: &mut SortStats,
    output_bytes: u64,
) -> Result<()> {
    job.manifest.state = JobState::Done;
    job.manifest.runs.clear();
    job.manifest.merge_done.clear();
    job.manifest.error.clear();
    stats.output_bytes = output_bytes;
    repo.commit_with_status(job, "done")?;
    Ok(())
}

// ===========================================================================
// Helpers
// ===========================================================================

fn drain_giant(scanner: &mut LineScanner) -> Result<()> {
    let mut buf = vec![0u8; crate::sortfile::CHUNK_MAX];
    loop {
        let p = scanner.pull(&mut buf)?;
        if p.end_of_record {
            return Ok(());
        }
        if p.bytes == 0 {
            return Err(Error::Fault("giant skip drain stalled".into()));
        }
    }
}

struct VFileReader {
    inner: Box<dyn VFile>,
}
impl Read for VFileReader {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        self.inner.read(buf).map_err(|e| match e {
            Error::Io(ioe, _) => ioe,
            other => std::io::Error::other(other.to_string()),
        })
    }
}
fn vfile_reader(f: Box<dyn VFile>) -> Box<dyn Read + Send> {
    Box::new(VFileReader { inner: f })
}

#[allow(dead_code)]
fn _keep_arc(_: Arc<dyn Vfs>) {}
