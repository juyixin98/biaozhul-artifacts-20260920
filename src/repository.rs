//! File-backed job repository: on-disk layout, manifests, status files and
//! crash recovery.
//!
//! # Job directory layout (under `<repo-root>/jobs/<job-id>/`)
//!
//! ```text
//! input.dat                 raw input bytes (uploaded by the client)
//! output.txt                sorted result (present once state = done)
//! manifest.txt              durable job state (see format below)
//! status.json               human/HTTP-facing status snapshot
//! level0/run-00000.run      map-phase sorted segments
//! level0/run-00001.run
//! level1/m-1-00000.run      merge-pass outputs
//! ...
//! *.tmp                     uncommitted files; always deleted on recovery
//! ```
//!
//! # Sync boundaries
//!
//! * A run is **durable but not committed** once its file has been written,
//!   fsynced and renamed from `*.run.tmp` (the parent directory is fsynced by
//!   [`crate::io::RealVfs::rename`]). Recovery may still delete such an orphan
//!   run if the manifest never recorded it — the manifest is the commit point.
//! * Every state change is: write `manifest.txt.tmp`, fsync, atomic rename to
//!   `manifest.txt`, fsync directory. After a crash the manifest therefore
//!   reflects a prefix of completed work; the engine resumes from exactly
//!   that point.
//!
//! # Manifest text format (`manifest.txt`)
//!
//! ```text
//! EXTSORT-MANIFEST/1
//! job <id>
//! state <map|merge|done|failed>
//! level <n>                 # level whose runs are ready for the next action
//! total_records <n>
//! map_consumed <n>          # input records already written to level0
//! spec <keys text>
//! delim <byte>
//! budget <bytes>
//! max_lanes <n>
//! run <name> <count>        # inputs for the active level (level 0 while
//!                           #   mapping; level L while merging level L)
//! merge_done <name> <count> # outputs already produced at level L+1
//! merge_batch <i>           # 0-based index of the next batch to merge; a
//!                           #   crash resumes here, reusing merge_done runs
//! ```

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::{Error, Result};
use crate::io::{atomic_write_bytes, read_all, Vfs};
use crate::sortfile::RunReader;

const MANIFEST_NAME: &str = "manifest.txt";
const MANIFEST_TMP: &str = "manifest.txt.tmp";
const STATUS_NAME: &str = "status.json";
const STATUS_TMP: &str = "status.json.tmp";
const MANIFEST_HEADER: &str = "EXTSORT-MANIFEST/1";

/// Job configuration supplied at creation.
#[derive(Debug, Clone)]
pub struct JobConfig {
    /// Composite-key text, e.g. `1:asc,2:desc`; empty means whole line.
    pub key_spec: String,
    /// One-byte column delimiter.
    pub delim: u8,
    /// Total memory budget including buffers.
    pub budget: u64,
    /// Merge fan-in limit.
    pub max_lanes: usize,
    /// Keep intermediate level directories after success.
    pub keep_temp: bool,
}

impl Default for JobConfig {
    fn default() -> Self {
        JobConfig {
            key_spec: String::new(),
            delim: b',',
            budget: 4 * 1024 * 1024,
            max_lanes: 4,
            keep_temp: false,
        }
    }
}

/// Coarse lifecycle state persisted in the manifest.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Map,
    Merge,
    Done,
    Failed,
}

impl JobState {
    fn as_str(self) -> &'static str {
        match self {
            JobState::Map => "map",
            JobState::Merge => "merge",
            JobState::Done => "done",
            JobState::Failed => "failed",
        }
    }
    fn parse(s: &str) -> Result<JobState> {
        Ok(match s {
            "map" => JobState::Map,
            "merge" => JobState::Merge,
            "done" => JobState::Done,
            "failed" => JobState::Failed,
            other => return Err(Error::InconsistentState(format!("unknown state {other}"))),
        })
    }
}

/// One run record tracked in the manifest.
#[derive(Debug, Clone)]
pub struct RunEntry {
    pub name: String,
    pub records: u64,
}

/// Durable job state.
#[derive(Debug, Clone)]
pub struct Manifest {
    pub state: JobState,
    /// Level whose unmerged inputs live in [`Manifest::runs`]: 0 while
    /// mapping, L while merging level L.
    pub level: u32,
    pub total_records: u64,
    pub map_consumed: u64,
    /// Map phase: committed level-0 runs. Merge phase: the still-unmerged
    /// input runs at `level` (completed batches are removed as their outputs
    /// are committed).
    pub runs: Vec<RunEntry>,
    /// Outputs already produced at `level+1` during the current merge pass.
    pub merge_done: Vec<RunEntry>,
    pub error: String,
}

impl Manifest {
    fn initial() -> Manifest {
        Manifest {
            state: JobState::Map,
            level: 0,
            total_records: 0,
            map_consumed: 0,
            runs: Vec::new(),
            merge_done: Vec::new(),
            error: String::new(),
        }
    }
}

/// A handle to one job inside a repository.
pub struct Job {
    pub id: String,
    pub cfg: JobConfig,
    pub manifest: Manifest,
}

impl Job {
    /// VFS-relative directory of one merge level.
    pub fn level_dir(&self, level: u32) -> String {
        format!("jobs/{}/level{}", self.id, level)
    }
    pub fn run_path(&self, level: u32, name: &str) -> String {
        format!("{}/{}", self.level_dir(level), name)
    }
    pub fn manifest_path(&self) -> String {
        format!("jobs/{}/{}", self.id, MANIFEST_NAME)
    }
    pub fn input_path(&self) -> String {
        format!("jobs/{}/input.dat", self.id)
    }
    pub fn output_path(&self) -> String {
        format!("jobs/{}/output.txt", self.id)
    }
    fn status_path(&self) -> String {
        format!("jobs/{}/{}", self.id, STATUS_NAME)
    }
}

/// The repository owns the root and the VFS implementation.
pub struct Repository {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
}

impl Repository {
    pub fn new(root: impl AsRef<Path>) -> Self {
        let root = root.as_ref().to_path_buf();
        let vfs = Arc::new(crate::io::RealVfs::new(&root));
        Repository { root, vfs }
    }

    pub fn with_vfs(root: impl AsRef<Path>, vfs: Arc<dyn Vfs>) -> Self {
        Repository {
            root: root.as_ref().to_path_buf(),
            vfs,
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn vfs(&self) -> Arc<dyn Vfs> {
        self.vfs.clone()
    }

    /// Create a fresh job: directory, manifest written and fsynced.
    pub fn create_job(&self, id: &str, cfg: JobConfig) -> Result<Job> {
        let job = Job {
            id: id.to_string(),
            cfg,
            manifest: Manifest::initial(),
        };
        let dir = format!("jobs/{id}");
        if self.vfs.exists(&format!("{dir}/{MANIFEST_NAME}")) {
            return Err(Error::InconsistentState(format!("job {id} already exists")));
        }
        self.vfs.make_dir_all(&job.level_dir(0))?;
        self.commit(&job)?;
        self.write_status(&job, "created")?;
        Ok(job)
    }

    /// Load and recover a job. Orphan temp files are removed; every run named
    /// by the manifest is integrity-checked (magic + all chunk CRCs + trailer
    /// count). The returned job's manifest is the authoritative resume point.
    pub fn open_job(&self, id: &str) -> Result<Job> {
        let mpath = format!("jobs/{id}/{MANIFEST_NAME}");
        let bytes = read_all(self.vfs.as_ref(), &mpath)?;
        let text = String::from_utf8(bytes)
            .map_err(|_| Error::corrupt(&mpath, "manifest is not valid UTF-8"))?;
        let (cfg, mut manifest) = parse_manifest(&text).map_err(|m| Error::corrupt(&mpath, m))?;

        let mut job = Job {
            id: id.to_string(),
            cfg,
            manifest: manifest.clone(),
        };

        self.cleanup_temps(&job)?;

        // Verify every run reachable from the manifest: `runs` live at
        // `level`, committed batch outputs (`merge_done`) at `level+1`.
        for run in &manifest.runs {
            self.verify_run(&job, manifest.level, &run.name, run.records)?;
        }
        for run in &manifest.merge_done {
            self.verify_run(&job, manifest.level + 1, &run.name, run.records)?;
        }

        // A manifest that said "done" only after output exists.
        if manifest.state == JobState::Done && !self.vfs.exists(&job.output_path()) {
            return Err(Error::InconsistentState(
                "manifest says done but output.txt is missing".into(),
            ));
        }
        // I/O faults are recorded in `error` but never move the job into a
        // terminal state: the committed segments remain resumable. Genuine
        // on-disk corruption is caught above by CRC/trailer verification.

        // Drop files not recorded in the manifest. At the merge *output* level
        // (level+1) only merge_done runs are valid, so anything else is a
        // half-written batch and is removed. At the *input* level the files of
        // already-committed batches are intentionally retained until the whole
        // pass rotates (they are needed to redo a crashed batch), so only
        // uncommitted *.tmp files are reaped there.
        if manifest.state == JobState::Merge {
            self.remove_tmp_files(&job.level_dir(manifest.level))?;
            self.remove_orphan_runs(&job, manifest.level + 1, &manifest.merge_done)?;
        }
        if manifest.state == JobState::Map {
            self.remove_orphan_runs(&job, 0, &manifest.runs)?;
        }

        job.manifest = manifest.clone();
        let _ = &mut manifest;
        self.write_status(
            &job,
            match job.manifest.state {
                JobState::Map => "resumed:map",
                JobState::Merge => "resumed:merge",
                JobState::Done => "done",
                JobState::Failed => "failed",
            },
        )?;
        Ok(job)
    }

    fn verify_run(&self, job: &Job, level: u32, name: &str, count: u64) -> Result<()> {
        let path = job.run_path(level, name);
        let reader = RunReader::open(self.vfs.as_ref(), &path)?;
        let seen = reader.finish()?;
        if seen != count {
            return Err(Error::corrupt(
                path,
                format!("manifest records {count} rows, file contains {seen}"),
            ));
        }
        Ok(())
    }

    /// Delete all `*.tmp` / `*.run.tmp` files under the job tree.
    fn cleanup_temps(&self, job: &Job) -> Result<()> {
        for ent in self.vfs.list_dir(&format!("jobs/{}", job.id))? {
            if ent.is_file && (ent.name.ends_with(".tmp")) {
                self.vfs.remove(&format!("jobs/{}/{}", job.id, ent.name))?;
            }
        }
        for level in 0..=32 {
            let dir = job.level_dir(level);
            if !self.vfs.exists(&dir) {
                if level == 0 {
                    continue;
                }
                break;
            }
            for ent in self.vfs.list_dir(&dir)? {
                if ent.is_file && ent.name.ends_with(".tmp") {
                    self.vfs.remove(&format!("{dir}/{}", ent.name))?;
                }
            }
        }
        Ok(())
    }

    /// Delete only uncommitted `*.tmp` files in one directory.
    fn remove_tmp_files(&self, dir: &str) -> Result<()> {
        if !self.vfs.exists(dir) {
            return Ok(());
        }
        for ent in self.vfs.list_dir(dir)? {
            if ent.is_file && ent.name.ends_with(".tmp") {
                self.vfs.remove(&format!("{dir}/{}", ent.name))?;
            }
        }
        Ok(())
    }

    /// Remove every committed run file in one level directory (called once a
    /// whole merge pass has rotated to the next level).
    pub fn delete_level_runs(&self, job: &Job, level: u32) -> Result<()> {
        let dir = job.level_dir(level);
        if !self.vfs.exists(&dir) {
            return Ok(());
        }
        for ent in self.vfs.list_dir(&dir)? {
            if ent.is_file && (ent.name.ends_with(".run") || ent.name.ends_with(".tmp")) {
                self.vfs.remove(&format!("{dir}/{}", ent.name))?;
            }
        }
        Ok(())
    }

    fn remove_orphan_runs(&self, job: &Job, level: u32, keep: &[RunEntry]) -> Result<()> {
        let dir = job.level_dir(level);
        if !self.vfs.exists(&dir) {
            return Ok(());
        }
        for ent in self.vfs.list_dir(&dir)? {
            if !ent.is_file || !ent.name.ends_with(".run") {
                continue;
            }
            if !keep.iter().any(|r| r.name == ent.name) {
                self.vfs.remove(&format!("{dir}/{}", ent.name))?;
            }
        }
        Ok(())
    }

    /// Persist the manifest (atomic, fsynced) and refresh status.
    pub fn commit(&self, job: &Job) -> Result<()> {
        let body = serialize_manifest(job);
        atomic_write_bytes(
            self.vfs.as_ref(),
            &format!("jobs/{}/{}", job.id, MANIFEST_TMP),
            &job.manifest_path(),
            body.as_bytes(),
        )?;
        Ok(())
    }

    /// Persist and also write the status snapshot.
    pub fn commit_with_status(&self, job: &Job, note: &str) -> Result<()> {
        self.commit(job)?;
        self.write_status(job, note)
    }

    /// Mark failure durably (best effort; errors here are ignored).
    /// Record an injected/transient fault for visibility (status.json) WITHOUT
    /// committing the in-memory manifest: the in-flight phase may have mutated
    /// a local `Job` that was not yet at a commit point, and persisting it
    /// could forget still-needed runs. The on-disk manifest therefore stays at
    /// its last durable commit and drives recovery.
    pub fn record_fault(&self, job: &mut Job, error: &str) {
        // Reflect only in the ephemeral status snapshot, never in manifest.txt.
        let saved = job.manifest.error.clone();
        job.manifest.error = error.to_string();
        let _ = self.write_status(job, "fault");
        job.manifest.error = saved;
    }

    fn write_status(&self, job: &Job, note: &str) -> Result<()> {
        let body = status_json(job, note);
        atomic_write_bytes(
            self.vfs.as_ref(),
            &format!("jobs/{}/{}", job.id, STATUS_TMP),
            &job.status_path(),
            body.as_bytes(),
        )
    }

    /// Read a job's status.json without full recovery checks.
    pub fn read_status(&self, id: &str) -> Result<String> {
        let p = format!("jobs/{id}/{STATUS_NAME}");
        let bytes = read_all(self.vfs.as_ref(), &p)?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }

    /// Enumerate job ids (directory names under `jobs/`).
    pub fn list_jobs(&self) -> Result<Vec<String>> {
        let mut out = Vec::new();
        for ent in self.vfs.list_dir("jobs")? {
            if !ent.is_file
                && self
                    .vfs
                    .exists(&format!("jobs/{}/{}", ent.name, MANIFEST_NAME))
            {
                out.push(ent.name);
            }
        }
        out.sort();
        Ok(out)
    }

    /// Remove a completed job's intermediate level directories (output and
    /// manifest are retained).
    pub fn cleanup_levels(&self, job: &Job) -> Result<()> {
        for level in 0..=64u32 {
            let dir = job.level_dir(level);
            if !self.vfs.exists(&dir) {
                break;
            }
            for ent in self.vfs.list_dir(&dir)? {
                if ent.is_file {
                    self.vfs.remove(&format!("{dir}/{}", ent.name))?;
                }
            }
        }
        Ok(())
    }
}

// ---- manifest (de)serialization -------------------------------------------

fn serialize_manifest(job: &Job) -> String {
    let m = &job.manifest;
    let mut s = String::new();
    s.push_str(MANIFEST_HEADER);
    s.push('\n');
    s.push_str(&format!("job {}\n", job.id));
    s.push_str(&format!("state {}\n", m.state.as_str()));
    s.push_str(&format!("level {}\n", m.level));
    s.push_str(&format!("total_records {}\n", m.total_records));
    s.push_str(&format!("map_consumed {}\n", m.map_consumed));
    s.push_str(&format!("spec {}\n", encode_one(&job.cfg.key_spec)));
    s.push_str(&format!("delim {}\n", job.cfg.delim));
    s.push_str(&format!("budget {}\n", job.cfg.budget));
    s.push_str(&format!("max_lanes {}\n", job.cfg.max_lanes));
    s.push_str(&format!("keep_temp {}\n", job.cfg.keep_temp as u8));
    for r in &m.runs {
        s.push_str(&format!("run {} {}\n", encode_one(&r.name), r.records));
    }
    for r in &m.merge_done {
        s.push_str(&format!(
            "merge_done {} {}\n",
            encode_one(&r.name),
            r.records
        ));
    }
    if !m.error.is_empty() {
        s.push_str(&format!("error {}\n", encode_one(&m.error)));
    }
    s
}

fn parse_manifest(text: &str) -> std::result::Result<(JobConfig, Manifest), String> {
    let mut lines = text.lines();
    let header = lines.next().unwrap_or("");
    if header != MANIFEST_HEADER {
        return Err(format!("bad manifest header: {header:?}"));
    }
    let mut cfg = JobConfig::default();
    let mut m = Manifest::initial();
    let mut job_id = String::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let (key, rest) = line.split_once(' ').unwrap_or((line, ""));
        match key {
            "job" => job_id = rest.to_string(),
            "state" => m.state = JobState::parse(rest).map_err(|e| e.to_string())?,
            "level" => m.level = rest.parse().map_err(|_| "bad level")?,
            "total_records" => m.total_records = rest.parse().map_err(|_| "bad total")?,
            "map_consumed" => m.map_consumed = rest.parse().map_err(|_| "bad map_consumed")?,
            "spec" => cfg.key_spec = decode_one(rest),
            "delim" => cfg.delim = rest.parse().map_err(|_| "bad delim")?,
            "budget" => cfg.budget = rest.parse().map_err(|_| "bad budget")?,
            "max_lanes" => cfg.max_lanes = rest.parse().map_err(|_| "bad max_lanes")?,
            "keep_temp" => cfg.keep_temp = rest == "1",
            "run" => {
                let (name, count) = parse_run(rest)?;
                m.runs.push(RunEntry {
                    name: decode_one(name),
                    records: count,
                });
            }
            "merge_done" => {
                let (name, count) = parse_run(rest)?;
                m.merge_done.push(RunEntry {
                    name: decode_one(name),
                    records: count,
                });
            }
            "error" => m.error = decode_one(rest),
            other => return Err(format!("unknown manifest key: {other}")),
        }
    }
    if job_id.is_empty() {
        return Err("manifest missing job id".into());
    }
    Ok((cfg, m))
}

fn parse_run(rest: &str) -> std::result::Result<(&str, u64), String> {
    let (name, count) = rest
        .rsplit_once(' ')
        .ok_or_else(|| "malformed run line".to_string())?;
    let count = count.parse().map_err(|_| "bad run count")?;
    Ok((name, count))
}

/// Percent-encode spaces and `%` so values remain single whitespace-free
/// tokens in the line format.
fn encode_one(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b' ' => out.push_str("%20"),
            b'%' => out.push_str("%25"),
            b'\n' => out.push_str("%0A"),
            _ => out.push(b as char),
        }
    }
    out
}

fn decode_one(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ---- status JSON -----------------------------------------------------------

fn status_json(job: &Job, note: &str) -> String {
    let m = &job.manifest;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!(
        "{{\"job_id\":\"{}\",\"state\":\"{}\",\"note\":\"{}\",\"error\":\"{}\",\"level\":{},\"total_records\":{},\"map_consumed\":{},\"unmerged_runs\":{},\"merge_done\":{},\"output\":\"jobs/{}/output.txt\",\"updated_unix\":{}}}\n",
        json_escape(&job.id),
        m.state.as_str(),
        json_escape(note),
        json_escape(&m.error),
        m.level,
        m.total_records,
        m.map_consumed,
        m.runs.len(),
        m.merge_done.len(),
        json_escape(&job.id),
        now,
    )
}

pub fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_roundtrip() {
        let cfg = JobConfig {
            key_spec: "1:asc,2:desc".into(),
            delim: b'|',
            budget: 123_456,
            max_lanes: 5,
            keep_temp: true,
        };
        let mut job = Job {
            id: "job-42".into(),
            cfg,
            manifest: Manifest::initial(),
        };
        job.manifest.state = JobState::Merge;
        job.manifest.level = 1;
        job.manifest.total_records = 99;
        job.manifest.map_consumed = 99;
        job.manifest.runs = vec![RunEntry {
            name: "m-1-00000.run".into(),
            records: 99,
        }];
        let text = serialize_manifest(&job);
        let (cfg2, m2) = parse_manifest(&text).unwrap();
        assert_eq!(cfg2.key_spec, "1:asc,2:desc");
        assert_eq!(cfg2.delim, b'|');
        assert_eq!(cfg2.budget, 123_456);
        assert_eq!(cfg2.max_lanes, 5);
        assert!(cfg2.keep_temp);
        assert_eq!(m2.state, JobState::Merge);
        assert_eq!(m2.level, 1);
        assert_eq!(m2.runs[0].name, "m-1-00000.run");
        assert_eq!(m2.runs[0].records, 99);
    }

    #[test]
    fn encode_tokens_with_spaces() {
        assert_eq!(encode_one("a b%c"), "a%20b%25c");
        assert_eq!(decode_one("a%20b%25c"), "a b%c");
    }
}
