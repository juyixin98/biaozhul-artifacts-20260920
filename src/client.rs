//! 协调服务 / 目标桩的小型 HTTP 客户端。中继、测试和示例脚本都用它，
//! 保证大家说的是同一套真实 HTTP+JSON 协议。

use serde_json::Value;

pub struct Client {
    base: String,
    http: reqwest::Client,
}

#[derive(Debug)]
pub struct ApiError {
    pub status: u16,
    pub body: String,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "HTTP {}: {}", self.status, self.body)
    }
}
impl std::error::Error for ApiError {}

impl Client {
    pub fn new(base: &str, timeout: std::time::Duration) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            http: reqwest::Client::builder().timeout(timeout).build().unwrap(),
        }
    }

    fn map_err(e: reqwest::Error) -> std::io::Error {
        std::io::Error::new(std::io::ErrorKind::TimedOut, e)
    }

    /// POST JSON，返回 (状态码, 响应体)。网络错误（含超时）转 io::Error，
    /// 调用方必须把它当"未知"处理，绝不能当失败。
    pub async fn post(&self, path: &str, body: Value) -> std::io::Result<(u16, Value)> {
        let resp = self
            .http
            .post(format!("{}{path}", self.base))
            .json(&body)
            .send()
            .await
            .map_err(Self::map_err)?;
        let status = resp.status().as_u16();
        let v: Value = resp.json().await.map_err(Self::map_err)?;
        Ok((status, v))
    }

    pub async fn get(&self, path: &str) -> std::io::Result<(u16, Value)> {
        let resp = self
            .http
            .get(format!("{}{path}", self.base))
            .send()
            .await
            .map_err(Self::map_err)?;
        let status = resp.status().as_u16();
        // 404 无 JSON body：直接返回 Null，让 get_opt 的 None 分支可达。
        if status == 404 {
            return Ok((404, Value::Null));
        }
        let v: Value = resp.json().await.map_err(Self::map_err)?;
        Ok((status, v))
    }

    pub async fn delete(&self, path: &str) -> std::io::Result<(u16, Value)> {
        let resp = self
            .http
            .delete(format!("{}{path}", self.base))
            .send()
            .await
            .map_err(Self::map_err)?;
        let status = resp.status().as_u16();
        let v: Value = resp.json().await.map_err(Self::map_err)?;
        Ok((status, v))
    }

    /// 404 也走 Ok 分支，用 None 表达 —— "未知"不是错误，更不是失败。
    pub async fn get_opt(&self, path: &str) -> std::io::Result<Option<Value>> {
        match self.get(path).await {
            Ok((404, _)) => Ok(None),
            Ok((_, v)) => Ok(Some(v)),
            Err(e) => Err(e),
        }
    }

    pub async fn post_empty(&self, path: &str) -> std::io::Result<u16> {
        let resp = self
            .http
            .post(format!("{}{path}", self.base))
            .send()
            .await
            .map_err(Self::map_err)?;
        Ok(resp.status().as_u16())
    }

    /// POST JSON：204 → None（无内容）；其余 2xx → 响应体；其它状态码 → ApiError。
    pub async fn post_opt(
        &self,
        path: &str,
        body: Value,
    ) -> std::io::Result<Result<Option<Value>, ApiError>> {
        let resp = self
            .http
            .post(format!("{}{path}", self.base))
            .json(&body)
            .send()
            .await
            .map_err(Self::map_err)?;
        let status = resp.status().as_u16();
        if status == 204 {
            return Ok(Ok(None));
        }
        let v: Value = resp.json().await.map_err(Self::map_err)?;
        if (200..300).contains(&status) {
            Ok(Ok(Some(v)))
        } else {
            Ok(Err(ApiError {
                status,
                body: v.to_string(),
            }))
        }
    }
}
