//! Bounded-memory external sort engine.
//!
//! Pipeline:
//!
//! 1. **Run formation (phase A).** Input is streamed record by record and
//!    records are held in memory until the configured budget is reached, then
//!    sorted in memory (stable sort by composite key) and spilled as a
//!    verified "run" segment (`jobs/<job>/runs/`). Every spill is followed by
//!    an fsync and a manifest checkpoint, so a crash leaves a prefix of
//!    complete runs.
//! 2. **Multi-way merge (phase B).** Runs are merged `k` at a time per level,
//!    producing new run-format files under `jobs/<job>/merges/`, until one
//!    file remains. Every merge output is fsynced and checkpointed.
//! 3. **Finalisation.** The last remaining run is streamed out to
//!    `jobs/<job>/output.txt` with newlines, fsynced, and the manifest flips
//!    to `state DONE`.
//!
//! Recovery on a subsequent call: verified run prefix + verified matching
//! merge outputs are reused; everything after the first invalid artifact is
//! rebuilt. Replay of the input skips records already covered by good runs.
//!
//! Memory accounting: `mem` covers the record arena *plus* one I/O buffer
//! (`buf`) for the writer/reader currently in use, with a safety margin for
//! allocator overhead. During merging, `k` read buffers and one write buffer
//! are reserved up front, so the effective fan-in is
//! `min(requested_k, (mem - margin) / buf - 1)`.

use std::sync::Arc;

use crate::error::{Error, Result};
use crate::format::{self, Record, RunInfo, RunStream, RunWriter};
use crate::fs::{BufWriter, Fs, WFile};
use crate::key::KeySpec;
use crate::lreader::LineReader;
use crate::manifest::{JobConfig, Manifest, MergeEntry, RunEntry};

/// User-facing sort configuration.
#[derive(Clone, Debug)]
pub struct SortConfig {
    pub key: KeySpec,
    /// Total memory budget in bytes (records + I/O buffers).
    pub mem: usize,
    /// I/O buffer size in bytes.
    pub buf: usize,
    /// Requested merge fan-in; may be lowered to fit the budget.
    pub fanin: Option<usize>,
}

impl SortConfig {
    pub fn new(mem: usize, buf: usize) -> Self {
        SortConfig { key: KeySpec::default_key(), mem, buf, fanin: None }
    }
}

/// Counters reported back through the HTTP response headers.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SortStats {
    pub input_records: u64,
    pub runs: u64,
    pub merge_rounds: u32,
    pub merge_steps: u64,
    pub runs_reused: u64,
    pub merges_reused: u64,
    pub fanin: usize,
    pub peak_bytes: usize,
    pub resumed: bool,
    pub output_rel: String,
}

pub struct Engine {
    fs: Arc<dyn Fs>,
}

impl Engine {
    pub fn new(fs: Arc<dyn Fs>) -> Self {
        Engine { fs }
    }

    pub fn fs(&self) -> &dyn Fs {
        self.fs.as_ref()
    }

    /// Runs (or resumes) the sort for `job_id`. The raw input must already be
    /// present at `jobs/<job>/input.txt`.
    pub fn run(&self, job_id: &str, cfg: &SortConfig) -> Result<SortStats> {
        validate_job_id(job_id)?;
        let fanin = effective_fanin(cfg.mem, cfg.buf, cfg.fanin.unwrap_or(8))?;

        let job_dir = format!("jobs/{job_id}");
        let runs_dir = format!("{job_dir}/runs");
        let merges_dir = format!("{job_dir}/merges");
        let input_rel = format!("{job_dir}/input.txt");
        let output_rel = format!("{job_dir}/output.txt");
        let manifest_rel = format!("{job_dir}/manifest.txt");
        let manifest_tmp = format!("{job_dir}/manifest.tmp");

        self.fs.create_dir_all(&runs_dir)?;
        self.fs.create_dir_all(&merges_dir)?;
        self.remove_tmp_files(&job_dir)?;

        if !self.fs.exists(&input_rel)? {
            return Err(Error::not_found(format!("input for job '{job_id}'")));
        }

        let recorded = JobConfig {
            key_canonical: cfg.key.canonical(),
            mem: cfg.mem,
            buf: cfg.buf,
            fanin,
        };

        let mut stats = SortStats {
            fanin,
            output_rel: output_rel.clone(),
            ..Default::default()
        };

        // ---- Load or create manifest -------------------------------------
        let mut manifest = match Manifest::load(self.fs.as_ref(), &manifest_rel)? {
            Some(m) => {
                stats.resumed = true;
                match &m.config {
                    Some(existing) if existing == &recorded => m,
                    Some(existing) => {
                        return Err(Error::conflict(format!(
                            "job '{job_id}' exists with config {existing:?}, requested {recorded:?}"
                        )));
                    }
                    None => return Err(Error::corrupt("manifest without config")),
                }
            }
            None => Manifest::new(recorded.clone()),
        };

        // Fast path: previously finished.
        if manifest.done && self.fs.exists(&output_rel)? {
            stats.peak_bytes = 0;
            return Ok(stats);
        }
        manifest.done = false;

        // ---- Validate run prefix; truncate after first bad run ------------
        let mut good_runs: Vec<RunEntry> = Vec::new();
        for entry in &manifest.runs {
            match self.verify_entry(entry) {
                Ok(()) => good_runs.push(entry.clone()),
                Err(e) => {
                    tracing_note(&format!("run {} not reusable: {e}", entry.rel));
                    break;
                }
            }
        }
        let prefix_truncated = good_runs.len() != manifest.runs.len();
        if prefix_truncated {
            // The run set changed: every prior merge output was derived from
            // it and must be discarded; phase B validates/redoes them.
            for m in &manifest.merges {
                let _ = self.fs.remove_file(&m.rel);
            }
            manifest.runs = good_runs;
            manifest.merges.clear();
            manifest.done = false;
            manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;
        }
        // Whether or not the manifest was truncated, delete every run file
        // beyond the surviving prefix: stale files from an aborted replay
        // would otherwise collide with new spills that reuse run indices and
        // could be mistaken for committed segments.
        let keep_indices: std::collections::HashSet<u64> =
            manifest.runs.iter().map(|r| r.index).collect();
        if self.fs.exists(&runs_dir)? {
            for name in self.fs.list(&runs_dir)? {
                if !name.starts_with("run-") || !name.ends_with(".exs") {
                    continue;
                }
                let index = name
                    .trim_start_matches("run-")
                    .trim_end_matches(".exs")
                    .parse::<u64>()
                    .unwrap_or(0);
                if !keep_indices.contains(&index) {
                    let _ = self.fs.remove_file(&format!("{runs_dir}/{name}"));
                }
            }
        }

        let already_ingested: u64 = manifest.runs.iter().map(|r| r.count).sum();

        // ---- Phase A: run formation ---------------------------------------
        let runs_at_entry = manifest.runs.len() as u64;
        let mut seq = already_ingested;
        let mut arena: Vec<Record> = Vec::new();
        let mut arena_bytes = 0usize;
        let mut peak_bytes = 0usize;

        let flush_threshold = cfg
            .mem
            .saturating_sub(cfg.buf)
            .saturating_sub(OVERHEAD_MARGIN);

        {
            let input = self.fs.open_read(&input_rel)?;
            let mut lines = LineReader::new(input, cfg.buf);
            // Skip records already persisted in good runs.
            for _ in 0..already_ingested {
                match lines.read_line()? {
                    Some(_) => {}
                    None => return Err(Error::corrupt("input shrank below ingested records")),
                }
            }

            while let Some(line) = lines.read_line()? {
                let len = line.len();
                arena.push(Record { seq, line });
                seq += 1;
                arena_bytes += record_size(len);
                if arena_bytes > peak_bytes {
                    peak_bytes = arena_bytes;
                }

                // Flush at the budget boundary. An arena that already holds a
                // single oversized record still flushes correctly on EOF.
                if arena_bytes >= flush_threshold {
                    let entry = self.flush_run(
                        &mut arena,
                        &mut arena_bytes,
                        &manifest,
                        &runs_dir,
                        cfg,
                    )?;
                    manifest.runs.push(entry);
                    manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;
                }
            }

            if !arena.is_empty() {
                let entry = self.flush_run(
                    &mut arena,
                    &mut arena_bytes,
                    &manifest,
                    &runs_dir,
                    cfg,
                )?;
                manifest.runs.push(entry);
                manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;
            }
        }

        // Empty input: no runs. Emit an empty output directly.
        stats.input_records = manifest.runs.iter().map(|r| r.count).sum();
        stats.runs = manifest.runs.len() as u64;
        stats.runs_reused = if stats.resumed { runs_at_entry } else { 0 };

        if manifest.runs.is_empty() {
            self.write_final_output(&output_rel, &[])?;
            manifest.merges.clear();
            manifest.done = true;
            manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;
            stats.peak_bytes = peak_bytes;
            return Ok(stats);
        }

        // ---- Phase B: k-way merge rounds ----------------------------------
        // Build the deterministic plan over the surviving run set.
        let plan = build_merge_plan(&manifest.runs, fanin, &merges_dir);

        // Recovery: merge entries are only trusted as a strict prefix of the
        // plan (same coordinates, name, inputs) whose files verify.
        let mut reusable_steps = 0usize;
        for (i, existing) in manifest.merges.iter().enumerate() {
            let ok = plan.get(i).is_some_and(|step| {
                step.round == existing.round
                    && step.step == existing.step
                    && step.rel == existing.rel
                    && step.inputs.len() == existing.inputs.len()
                    && step.inputs.iter().zip(&existing.inputs).all(|(a, b)| a == b)
                    && self.verify_manifest_merge(existing).is_ok()
            });
            if !ok {
                break;
            }
            reusable_steps += 1;
        }
        if reusable_steps != manifest.merges.len() {
            for m in &manifest.merges[reusable_steps..] {
                let _ = self.fs.remove_file(&m.rel);
            }
            manifest.merges.truncate(reusable_steps);
            manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;
        }

        // Resume each merge level from the verified prefix. While walking a
        // level's steps in order, `level_inputs` is the queue of that level's
        // input files; each step (done or not) pops its group off the front,
        // and completed steps still contribute their output to the next level.
        let rounds: Vec<u32> = {
            let mut v: Vec<u32> = plan.iter().map(|s| s.round).collect();
            v.dedup();
            v
        };
        let mut plan_cursor = 0usize; // index into `plan`
        let mut committed = reusable_steps;
        let mut level_inputs: Vec<String> =
            manifest.runs.iter().map(|r| r.rel.clone()).collect();
        let mut merge_peak: usize = (fanin + 1) * cfg.buf;

        for &round in &rounds {
            let level_step_count = plan.iter().filter(|s| s.round == round).count();
            let mut next_inputs: Vec<String> = Vec::new();
            for _ in 0..level_step_count {
                let step = &plan[plan_cursor];
                debug_assert_eq!(step.round, round);

                if committed > 0 {
                    // This step was already committed in the reusable prefix.
                    if step.inputs.len() > level_inputs.len()
                        || level_inputs[..step.inputs.len()] != step.inputs[..]
                    {
                        return Err(Error::corrupt(
                            "merge prefix inputs diverge from plan; cannot resume",
                        ));
                    }
                    level_inputs.drain(..step.inputs.len());
                    next_inputs.push(step.rel.clone());
                    committed -= 1;
                } else {
                    if step.inputs.len() > level_inputs.len()
                        || level_inputs[..step.inputs.len()] != step.inputs[..]
                    {
                        return Err(Error::corrupt("merge inputs diverge from plan"));
                    }
                    let est = (step.inputs.len() + 1) * cfg.buf;
                    if est > merge_peak {
                        merge_peak = est;
                    }
                    let info = self.do_merge(step, cfg, fanin)?;
                    manifest.merges.push(MergeEntry {
                        round: step.round,
                        step: step.step,
                        rel: step.rel.clone(),
                        count: info.count,
                        aggr_crc: info.aggr_crc,
                        inputs: step.inputs.clone(),
                    });
                    manifest.save(
                        self.fs.as_ref(),
                        &manifest_rel,
                        &manifest_tmp,
                        &job_dir,
                        cfg.buf,
                    )?;
                    level_inputs.drain(..step.inputs.len());
                    next_inputs.push(step.rel.clone());
                }
                plan_cursor += 1;
            }
            level_inputs = next_inputs;
        }
        debug_assert_eq!(plan_cursor, plan.len());
        if merge_peak > peak_bytes {
            peak_bytes = merge_peak;
        }

        stats.merge_rounds = rounds.len() as u32;
        stats.merge_steps = plan.len() as u64;
        stats.merges_reused = reusable_steps as u64;

        // ---- Finalise: stream the sole surviving run to output.txt --------
        let final_rel = final_survivor(&manifest, &plan);
        self.materialize_output(&final_rel, &output_rel, cfg)?;

        manifest.done = true;
        manifest.save(self.fs.as_ref(), &manifest_rel, &manifest_tmp, &job_dir, cfg.buf)?;

        stats.peak_bytes = peak_bytes;
        Ok(stats)
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    fn verify_entry(&self, e: &RunEntry) -> Result<()> {
        let info = format::verify_run(self.fs.as_ref(), &e.rel, 4096)?;
        if info.count != e.count || info.aggr_crc != e.aggr_crc {
            return Err(Error::corrupt(format!(
                "{}: manifest summary {}/{} mismatches file {}/{}",
                e.rel, e.count, e.aggr_crc, info.count, info.aggr_crc
            )));
        }
        Ok(())
    }

    fn verify_manifest_merge(&self, m: &MergeEntry) -> Result<()> {
        let info = format::verify_run(self.fs.as_ref(), &m.rel, 4096)?;
        if info.count != m.count || info.aggr_crc != m.aggr_crc {
            return Err(Error::corrupt(format!("{}: merge summary mismatch", m.rel)));
        }
        Ok(())
    }

    fn flush_run(
        &self,
        arena: &mut Vec<Record>,
        arena_bytes: &mut usize,
        manifest: &Manifest,
        runs_dir: &str,
        cfg: &SortConfig,
    ) -> Result<RunEntry> {
        // Stable sort: equal keys preserve their input (seq) order.
        arena.sort_by(|a, b| cfg.key.cmp(a, b));

        let index = (manifest.runs.len() as u64) + 1;
        let final_name = format!("{runs_dir}/run-{index:06}.exs");
        let tmp_name = format!("{runs_dir}/run-{index:06}.tmp");
        let _ = self.fs.remove_file(&tmp_name);

        let mut writer = RunWriter::create(self.fs.as_ref(), &tmp_name, cfg.buf)?;
        for rec in arena.iter() {
            writer.write_record(rec)?;
        }
        let info = writer.finish()?; // fsync of data
        self.fs.rename(&tmp_name, &final_name)?;
        self.fs.sync_dir(runs_dir)?;

        arena.clear();
        *arena_bytes = 0;
        Ok(RunEntry { index, rel: final_name, count: info.count, aggr_crc: info.aggr_crc })
    }

    fn do_merge(&self, step: &MergeStep, cfg: &SortConfig, k: usize) -> Result<RunInfo> {
        let _ = k;
        let tmp = format!("{}.tmp", step.rel);
        let _ = self.fs.remove_file(&tmp);

        let mut cursors: Vec<Cursor> = Vec::with_capacity(step.inputs.len());
        for rel in &step.inputs {
            let mut stream = RunStream::open(self.fs.as_ref(), rel, cfg.buf)?;
            let first = stream.next_record()?;
            cursors.push(Cursor { stream, current: first });
        }

        let mut writer = RunWriter::create(self.fs.as_ref(), &tmp, cfg.buf)?;
        loop {
            // Select the smallest current record under the composite key;
            // stability between runs is guaranteed by seq in the comparator.
            let mut pick: Option<usize> = None;
            for (i, c) in cursors.iter().enumerate() {
                if let Some(rec) = &c.current {
                    match pick {
                        None => pick = Some(i),
                        Some(j) => {
                            if cfg.key.cmp(rec, cursors[j].current.as_ref().unwrap()).is_lt() {
                                pick = Some(i);
                            }
                        }
                    }
                }
            }
            let i = match pick {
                Some(i) => i,
                None => break,
            };
            let rec = cursors[i].current.take().unwrap();
            writer.write_record(&rec)?;
            cursors[i].current = cursors[i].stream.next_record()?;
        }
        let info = writer.finish()?;
        self.fs.rename(&tmp, &step.rel)?;
        self.fs.sync_dir(parent_dir(&step.rel))?;
        Ok(info)
    }

    fn materialize_output(&self, final_run_rel: &str, output_rel: &str, cfg: &SortConfig) -> Result<()> {
        let tmp = format!("{output_rel}.tmp");
        let _ = self.fs.remove_file(&tmp);

        let mut stream = RunStream::open(self.fs.as_ref(), final_run_rel, cfg.buf)?;
        let raw = self.fs.create_write(&tmp)?;
        let mut out = BufWriter::new(raw, cfg.buf);
        while let Some(rec) = stream.next_record()? {
            out.write_all(&rec.line)?;
            out.write_all(b"\n")?;
        }
        out.sync_all()?;
        drop(out);
        self.fs.rename(&tmp, output_rel)?;
        self.fs.sync_dir(parent_dir(output_rel))?;
        Ok(())
    }

    fn write_final_output(&self, output_rel: &str, _records: &[Record]) -> Result<()> {
        let tmp = format!("{output_rel}.tmp");
        let _ = self.fs.remove_file(&tmp);
        let mut raw = self.fs.create_write(&tmp)?;
        raw.sync_all()?;
        drop(raw);
        self.fs.rename(&tmp, output_rel)?;
        self.fs.sync_dir(parent_dir(output_rel))?;
        Ok(())
    }

    fn remove_tmp_files(&self, job_dir: &str) -> Result<()> {
        for sub in ["runs", "merges"] {
            let dir = format!("{job_dir}/{sub}");
            if !self.fs.exists(&dir)? {
                continue;
            }
            for name in self.fs.list(&dir)? {
                if name.ends_with(".tmp") {
                    self.fs.remove_file(&format!("{dir}/{name}"))?;
                }
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Pure planning / sizing helpers (unit-testable without a filesystem)
// ---------------------------------------------------------------------------

const OVERHEAD_MARGIN: usize = 256;

struct Cursor {
    stream: RunStream,
    current: Option<Record>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MergeStep {
    pub round: u32,
    pub step: u32,
    pub rel: String,
    pub inputs: Vec<String>,
}

/// Per-record conservative size: bytes + Vec/Record overhead.
fn record_size(line_len: usize) -> usize {
    // Record { seq:8, line:Vec(24) } plus the line allocation rounded up a bit.
    48 + line_len + line_len / 8
}

fn effective_fanin(mem: usize, buf: usize, requested: usize) -> Result<usize> {
    if buf == 0 {
        return Err(Error::bad("buf must be > 0"));
    }
    // Need room for at least two I/O buffers (one reader + one writer).
    if !buf.checked_mul(2).is_some_and(|need| mem >= need) {
        return Err(Error::bad(format!(
            "mem={mem} too small: need at least 2*buf ({})",
            buf.saturating_mul(2)
        )));
    }
    if requested < 2 {
        return Err(Error::bad("fanin must be >= 2"));
    }
    // Reserve the overhead margin only when the budget allows; otherwise the
    // floor of 2 still leaves the whole remaining budget to records.
    let reserved = if mem >= buf * 2 + OVERHEAD_MARGIN {
        OVERHEAD_MARGIN
    } else {
        0
    };
    let by_mem = (mem - reserved) / buf - 1; // k read buffers + 1 write buffer
    Ok(requested.min(by_mem).max(2))
}

fn validate_job_id(job: &str) -> Result<()> {
    if job.is_empty()
        || job.len() > 128
        || !job
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err(Error::bad("job id must be [A-Za-z0-9_-]{1,128}"));
    }
    Ok(())
}

fn parent_dir(rel: &str) -> &str {
    match rel.rfind('/') {
        Some(i) => &rel[..i],
        None => ".",
    }
}

/// Builds the full deterministic merge plan: level 0 merges runs, later
/// levels merge previous level outputs. A level with a single input produces
/// no step (that file passes through).
pub fn build_merge_plan(runs: &[RunEntry], k: usize, merges_dir: &str) -> Vec<MergeStep> {
    let mut plan = Vec::new();
    let mut current: Vec<String> = runs.iter().map(|r| r.rel.clone()).collect();
    let mut round = 0u32;
    while current.len() > 1 {
        let mut next: Vec<String> = Vec::new();
        let mut step = 0u32;
        let mut chunks: Vec<Vec<String>> = Vec::new();
        let mut group: Vec<String> = Vec::new();
        for rel in current.drain(..) {
            group.push(rel);
            if group.len() == k {
                chunks.push(std::mem::take(&mut group));
            }
        }
        if !group.is_empty() {
            chunks.push(group);
        }
        for inputs in chunks {
            let rel = format!("{merges_dir}/m-r{round:02}-s{step:03}.exs");
            next.push(rel.clone());
            plan.push(MergeStep { round, step, rel, inputs });
            step += 1;
        }
        current = next;
        round += 1;
    }
    plan
}

fn final_survivor(manifest: &Manifest, plan: &[MergeStep]) -> String {
    if let Some(last) = plan.last() {
        last.rel.clone()
    } else {
        manifest.runs[0].rel.clone()
    }
}

fn tracing_note(msg: &str) {
    // Deliberately tiny: surface recovery decisions via stderr in tests/runs
    // without pulling a logging dependency.
    eprintln!("[extsort] {msg}");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run_entry(index: u64, count: u64) -> RunEntry {
        RunEntry { index, rel: format!("runs/run-{index:06}.exs"), count, aggr_crc: 0 }
    }

    #[test]
    fn fanin_respects_budget() {
        assert!(effective_fanin(100, 64, 8).is_err());
        // (4096 - 256) / 512 - 1 = 6
        assert_eq!(effective_fanin(4096, 512, 8).unwrap(), 6);
        // request wins when smaller
        assert_eq!(effective_fanin(4096, 512, 3).unwrap(), 3);
        // tight budget: floor of 2 even when 2*buf ~= mem
        assert_eq!(effective_fanin(4096, 2048, 8).unwrap(), 2);
        assert!(effective_fanin(4096, 4096, 8).is_err());
    }

    #[test]
    fn plan_rounds_and_pass_through() {
        let runs: Vec<RunEntry> = (1..=10).map(|i| run_entry(i, 1)).collect();
        let plan = build_merge_plan(&runs, 3, "merges");
        // level0: ceil(10/3)=4, level1: ceil(4/3)=2, level2: 1
        let rounds: Vec<u32> = plan.iter().map(|s| s.round).collect();
        assert_eq!(plan.len(), 4 + 2 + 1);
        assert_eq!(*rounds.last().unwrap(), 2);
        // single run -> no merges
        let one = build_merge_plan(&[run_entry(1, 1)], 3, "merges");
        assert!(one.is_empty());
        // exactly k -> one step
        let two = build_merge_plan(&[run_entry(1, 1), run_entry(2, 1)], 2, "merges");
        assert_eq!(two.len(), 1);
    }

    #[test]
    fn job_id_validation() {
        assert!(validate_job_id("job_1-2").is_ok());
        assert!(validate_job_id("").is_err());
        assert!(validate_job_id("../escape").is_err());
        assert!(validate_job_id("a/b").is_err());
    }
}
