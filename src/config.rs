use std::path::PathBuf;

/// 服务运行配置。
#[derive(Debug, Clone)]
pub struct Config {
    /// 数据目录（MANIFEST 与 segments/ 都放在这里）。
    pub dir: PathBuf,
    /// memtable 条目数达到该阈值即冻结为 immutable memtable。
    pub memtable_entries: usize,
    /// 是否启动后台 flush 线程；测试中关闭以获得确定性。
    pub background_flush: bool,
}

impl Config {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self {
            dir: dir.into(),
            memtable_entries: 1000,
            background_flush: true,
        }
    }

    pub fn memtable_entries(mut self, n: usize) -> Self {
        self.memtable_entries = n.max(1);
        self
    }

    pub fn background_flush(mut self, on: bool) -> Self {
        self.background_flush = on;
        self
    }
}
