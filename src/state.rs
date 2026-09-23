//! 宿主 KV 状态：原子快照发布。
//!
//! 每次执行开始时拿到当前快照的 `Arc`；执行期间所有 `kv_get/kv_has`
//! 都只读取该快照（看不到自己未提交的写入），写入全部进入私有事务缓冲。
//! 执行成功后用 CAS 把缓冲合并进最新快照并整体替换 `Arc`——
//! 对外部观察者而言发布是原子的。失败则缓冲直接丢弃。

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

#[derive(Debug, Default)]
pub struct HostState {
    snapshot: Mutex<Arc<BTreeMap<String, Vec<u8>>>>,
}

impl HostState {
    pub fn new() -> Self {
        Self {
            snapshot: Mutex::new(Arc::new(BTreeMap::new())),
        }
    }

    /// 预置种子数据（仅供演示/测试，不经过事务）。
    pub fn seed(&self, key: impl Into<String>, value: impl Into<Vec<u8>>) {
        let mut guard = self.snapshot.lock().unwrap();
        let mut next = (**guard).clone();
        next.insert(key.into(), value.into());
        *guard = Arc::new(next);
    }

    pub fn current(&self) -> Arc<BTreeMap<String, Vec<u8>>> {
        Arc::clone(&*self.snapshot.lock().unwrap())
    }

    /// 原子发布一批写入。返回按 key 排序的 (key, hex(value)) 列表。
    ///
    /// 合并基于当前最新快照，因此多次调用（如多个成功的 HTTP 请求串行发布）
    /// 不会互相覆盖整体快照；单次执行内部的事务缓冲在发布前对任何人不可见。
    pub fn commit(&self, writes: &BTreeMap<String, Vec<u8>>) -> Vec<(String, String)> {
        let mut guard = self.snapshot.lock().unwrap();
        let mut next = (**guard).clone();
        for (k, v) in writes {
            next.insert(k.clone(), v.clone());
        }
        let summary = writes
            .iter()
            .map(|(k, v)| (k.clone(), hex::encode(v)))
            .collect();
        *guard = Arc::new(next);
        summary
    }

    /// 当前 KV 条目数（健康检查/测试用）。
    pub fn len(&self) -> usize {
        self.snapshot.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}
