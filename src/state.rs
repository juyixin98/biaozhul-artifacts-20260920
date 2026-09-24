//! 服务共享状态与每上传会话互斥锁。
use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::store::Store;

/// 每会话一把锁：同一 upload_id 的 PUT / finish 串行化，
/// 不同 upload_id 之间完全并行。锁惰性创建、不回收（会话数量级别可接受；
/// 长生命周期服务可在 finish 后清理，此处保持简单正确）。
#[derive(Default)]
pub struct UploadLocks {
    inner: tokio::sync::Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl UploadLocks {
    pub async fn acquire(&self, upload_id: &str) -> Arc<Mutex<()>> {
        let mut map = self.inner.lock().await;
        map.entry(upload_id.to_string())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }
}

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    pub locks: Arc<UploadLocks>,
    pub max_chunk_size: u64,
}

impl AppState {
    pub async fn new(data_dir: &str, max_chunk_size: u64) -> crate::error::AppResult<Self> {
        let store = Store::new(data_dir).await?;
        Ok(Self {
            store,
            locks: Arc::new(UploadLocks::default()),
            max_chunk_size,
        })
    }
}
