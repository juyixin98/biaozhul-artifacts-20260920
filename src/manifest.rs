//! Job manifest: the authoritative recovery state.
//!
//! Stored at `jobs/<job>/manifest.txt` and rewritten atomically (temp +
//! fsync + rename + dir-fsync) on every state transition. The file is plain
//! text; its shape is shown below.
//!
//! ```text
//! v1
//! key=<canonical key spec>
//! mem=<bytes>
//! buf=<bytes>
//! fanin=<effective k>
//! run 000001 runs/run-000001.tmp final-name runs/run-000001.exs <count> <aggr_crc>
//! merge r00 000001 merges/... <count> <aggr_crc> inputs=runs/run-000001.exs;...
//! state DONE
//! ```
//!
//! Recovery rule: runs form an ordered prefix. If a run is missing on disk or
//! fails verification, it and *every later run plus all merge entries* are
//! discarded, and the engine replays the input from the record following the
//! last good run. Merge entries are only trusted when their referenced inputs
//! match the deterministic merge plan for the surviving run set and the merge
//! output itself verifies; otherwise the whole merge phase is redone.

use crate::error::{Error, Result};
use crate::fs::{BufWriter, Fs, WFile};

const MANIFEST_VERSION: &str = "v1";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunEntry {
    pub index: u64,
    pub rel: String,
    pub count: u64,
    pub aggr_crc: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MergeEntry {
    pub round: u32,
    pub step: u32,
    pub rel: String,
    pub count: u64,
    pub aggr_crc: u32,
    pub inputs: Vec<String>,
}

/// Immutable configuration recorded with the job; a resumed run must match it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobConfig {
    pub key_canonical: String,
    pub mem: usize,
    pub buf: usize,
    pub fanin: usize,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Manifest {
    pub config: Option<JobConfig>,
    pub runs: Vec<RunEntry>,
    pub merges: Vec<MergeEntry>,
    pub done: bool,
}

impl Manifest {
    pub fn new(config: JobConfig) -> Self {
        Manifest { config: Some(config), runs: Vec::new(), merges: Vec::new(), done: false }
    }

    pub fn config(&self) -> Result<&JobConfig> {
        self.config.as_ref().ok_or_else(|| Error::corrupt("manifest missing config"))
    }

    /// Serialises and atomically replaces the manifest (write → fsync → rename
    /// → dir-fsync).
    pub fn save(&self, fs: &dyn Fs, manifest_rel: &str, tmp_rel: &str, dir_rel: &str, io_buf: usize) -> Result<()> {
        let body = self.serialize();
        let raw = fs.create_write(tmp_rel)?;
        let mut w = BufWriter::new(raw, io_buf);
        w.write_all(body.as_bytes())?;
        w.sync_all()?;
        drop(w);
        fs.rename(tmp_rel, manifest_rel)?;
        fs.sync_dir(dir_rel)?;
        Ok(())
    }

    fn serialize(&self) -> String {
        let mut s = String::new();
        s.push_str(MANIFEST_VERSION);
        s.push('\n');
        if let Some(c) = &self.config {
            s.push_str(&format!("key={}\n", c.key_canonical));
            s.push_str(&format!("mem={}\n", c.mem));
            s.push_str(&format!("buf={}\n", c.buf));
            s.push_str(&format!("fanin={}\n", c.fanin));
        }
        for r in &self.runs {
            s.push_str(&format!(
                "run {:06} {} {} {}\n",
                r.index, r.rel, r.count, r.aggr_crc
            ));
        }
        for m in &self.merges {
            s.push_str(&format!(
                "merge r{:02} {:06} {} {} {} inputs={}\n",
                m.round,
                m.step,
                m.rel,
                m.count,
                m.aggr_crc,
                m.inputs.join(";")
            ));
        }
        if self.done {
            s.push_str("state DONE\n");
        }
        s
    }

    /// Strict parser. Any malformed line is a corruption error; callers
    /// recovering from a torn manifest handle that at a higher level.
    pub fn parse(body: &str) -> Result<Self> {
        let mut lines = body.split('\n');
        let version = lines.next().unwrap_or("");
        if version != MANIFEST_VERSION {
            return Err(Error::corrupt(format!("unsupported manifest version: {version:?}")));
        }

        let mut m = Manifest::default();
        let mut saw_state = false;
        for (lineno, line) in lines.enumerate().map(|(i, l)| (i + 2, l)) {
            if line.is_empty() {
                continue; // trailing newline only
            }
            let bad = || Error::corrupt(format!("manifest line {lineno}: {line:?}"));

            if let Some(v) = line.strip_prefix("key=") {
                if m.config.is_some() {
                    return Err(bad());
                }
                m.config = Some(JobConfig {
                    key_canonical: v.to_owned(),
                    mem: 0,
                    buf: 0,
                    fanin: 0,
                });
            } else if let Some(v) = line.strip_prefix("mem=") {
                let c = m.config.as_mut().ok_or_else(bad)?;
                c.mem = v.parse().map_err(|_| bad())?;
            } else if let Some(v) = line.strip_prefix("buf=") {
                let c = m.config.as_mut().ok_or_else(bad)?;
                c.buf = v.parse().map_err(|_| bad())?;
            } else if let Some(v) = line.strip_prefix("fanin=") {
                let c = m.config.as_mut().ok_or_else(bad)?;
                c.fanin = v.parse().map_err(|_| bad())?;
            } else if let Some(rest) = line.strip_prefix("run ") {
                let f: Vec<&str> = rest.split(' ').collect();
                if f.len() != 4 {
                    return Err(bad());
                }
                m.runs.push(RunEntry {
                    index: f[0].parse().map_err(|_| bad())?,
                    rel: f[1].to_owned(),
                    count: f[2].parse().map_err(|_| bad())?,
                    aggr_crc: f[3].parse().map_err(|_| bad())?,
                });
            } else if let Some(rest) = line.strip_prefix("merge ") {
                // merge r00 000001 rel count crc inputs=a;b
                let f: Vec<&str> = rest.split(' ').collect();
                if f.len() != 6 || !f[0].starts_with('r') {
                    return Err(bad());
                }
                let round: u32 = f[0][1..].parse().map_err(|_| bad())?;
                let step: u32 = f[1].parse().map_err(|_| bad())?;
                let inputs: Vec<String> = f[5]
                    .strip_prefix("inputs=")
                    .ok_or_else(bad)?
                    .split(';')
                    .filter(|s| !s.is_empty())
                    .map(|s| s.to_owned())
                    .collect();
                m.merges.push(MergeEntry {
                    round,
                    step,
                    rel: f[2].to_owned(),
                    count: f[3].parse().map_err(|_| bad())?,
                    aggr_crc: f[4].parse().map_err(|_| bad())?,
                    inputs,
                });
            } else if line == "state DONE" {
                if saw_state {
                    return Err(bad());
                }
                saw_state = true;
                m.done = true;
            } else {
                return Err(bad());
            }
        }

        // Config consistency: either fully present or absent.
        if let Some(c) = &m.config {
            if c.mem == 0 || c.buf == 0 || c.fanin == 0 {
                return Err(Error::corrupt("manifest config incomplete"));
            }
        }
        Ok(m)
    }

    /// Loads the manifest if present; a missing manifest yields the default
    /// (fresh job). A corrupt manifest is an error rather than silently
    /// discarded — the caller decides policy.
    pub fn load(fs: &dyn Fs, rel: &str) -> Result<Option<Manifest>> {
        if !fs.exists(rel)? {
            return Ok(None);
        }
        let mut file = fs.open_read(rel)?;
        let mut body = Vec::new();
        let mut chunk = [0u8; 8192];
        loop {
            let n = file.read(&mut chunk)?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..n]);
        }
        let body = String::from_utf8(body).map_err(|_| Error::corrupt("manifest not UTF-8"))?;
        Ok(Some(Manifest::parse(&body)?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> JobConfig {
        JobConfig { key_canonical: "0:asc".into(), mem: 4096, buf: 512, fanin: 4 }
    }

    #[test]
    fn roundtrip() {
        let mut m = Manifest::new(cfg());
        m.runs.push(RunEntry { index: 1, rel: "runs/x".into(), count: 3, aggr_crc: 42 });
        m.merges.push(MergeEntry {
            round: 0,
            step: 0,
            rel: "merges/y".into(),
            count: 3,
            aggr_crc: 7,
            inputs: vec!["runs/a".into(), "runs/b".into()],
        });
        let body = m.serialize();
        let p = Manifest::parse(&body).unwrap();
        assert_eq!(p, m);
        assert!(!p.done);
        m.done = true;
        assert!(Manifest::parse(&m.serialize()).unwrap().done);
    }

    #[test]
    fn rejects_garbage() {
        assert!(Manifest::parse("v2\n").is_err());
        assert!(Manifest::parse("v1\nrun x y\n").is_err());
        assert!(Manifest::parse("v1\nmem=10\n").is_err()); // config incomplete
        assert!(Manifest::parse("v1\nstate DONE\nstate DONE\n").is_err());
    }
}
