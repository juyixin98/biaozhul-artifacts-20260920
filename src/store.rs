//! 日志仓库:管理数据根目录下的多个命名日志。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::log::{Log, LogError};

pub struct Store {
    root: PathBuf,
    max_segment_size: u64,
    logs: Mutex<BTreeMap<String, Arc<Log>>>,
}

fn validate_name(name: &str) -> Result<(), LogError> {
    let ok = !name.is_empty()
        && name.len() <= 64
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if ok {
        Ok(())
    } else {
        Err(LogError::InvalidName(name.to_string()))
    }
}

impl Store {
    /// 打开仓库:扫描根目录,逐个打开已有日志并执行崩溃恢复。
    /// 任何一个日志损坏都导致整体拒绝启动。
    pub fn open(root: &Path, max_segment_size: u64) -> Result<Store, LogError> {
        fs::create_dir_all(root)?;
        let mut logs = BTreeMap::new();
        for entry in fs::read_dir(root)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                let name = entry.file_name().to_string_lossy().into_owned();
                let log = Log::open(&entry.path(), max_segment_size).map_err(|e| {
                    eprintln!("[seglog] 打开日志 {name:?} 失败: {e}");
                    e
                })?;
                logs.insert(name, Arc::new(log));
            }
        }
        Ok(Store { root: root.to_path_buf(), max_segment_size, logs: Mutex::new(logs) })
    }

    /// 读取已有日志(不存在则报错)。
    pub fn get(&self, name: &str) -> Result<Arc<Log>, LogError> {
        validate_name(name)?;
        self.logs
            .lock()
            .unwrap()
            .get(name)
            .cloned()
            .ok_or_else(|| LogError::LogNotFound(name.to_string()))
    }

    /// 读取或创建日志。
    pub fn get_or_create(&self, name: &str) -> Result<Arc<Log>, LogError> {
        validate_name(name)?;
        let mut logs = self.logs.lock().unwrap();
        if let Some(l) = logs.get(name) {
            return Ok(l.clone());
        }
        let log = Arc::new(Log::open(&self.root.join(name), self.max_segment_size)?);
        logs.insert(name.to_string(), log.clone());
        Ok(log)
    }
}
