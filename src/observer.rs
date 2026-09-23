//! 增量观察器：喂入任意切分的 TCP 字节流，观察明文 ClientHello。
//!
//! ## 状态机
//!
//! ```text
//! AwaitHello ──22(handshake)──► 重组 u24 握手消息 ──► 第一条消息
//!   │                                                ├─ ClientHello → Done
//!   │                                                └─ 其它类型     → NoHello
//!   ├─20/21(CCS/Alert)──────────► 非致命记录（继续等待明文握手）
//!   └─23(app_data)──────────────► 此前无握手数据 → NoHello(EncryptedData)
//!
//! Done / NoHello / Failed 为终态；Done 之后的所有记录一律标 encrypted，
//! 绝不会再把密文送进握手解析器。
//! ```
//!
//! ## 不做什么
//! 不解密、不实现握手、不发送任何数据、不推断记录层之后的内容；
//! 非握手记录的 fragment 只按不透明字节计数。

use crate::client_hello::ClientHelloInfo;
use crate::config::Config;
use crate::error::ParseError;
use crate::record::{content_type_name, RecordHeader, CONTENT_HANDSHAKE};

/// 一条已消费记录的观察结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordObservation {
    /// 该记录在本次连接字节流中的序号（从 0 开始）。
    pub index: usize,
    pub content_type: u8,
    pub content_type_name: &'static str,
    pub version: u16,
    pub fragment_len: usize,
    /// 该记录是否被当作**明文**握手数据参与了解析。
    /// false 表示其 fragment 被视为不透明字节（通常是密文/协议杂项）。
    pub parsed_as_plaintext: bool,
}

/// 未观察到 ClientHello 的原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NoHelloReason {
    /// 第一条握手消息不是 ClientHello（给出 HandshakeType）。
    FirstHandshakeNotClientHello { handshake_type: u8 },
    /// 握手消息重组途中插入了非握手记录（CCS/Alert/AppData）——
    /// 明文结构被破坏或加密已开始，拒绝继续猜测。
    RecordInterleaved { content_type: u8 },
    /// 连接里没有握手记录，直接出现了加密数据（app_data）。
    EncryptedDataWithoutHandshake,
    /// 连接结束前一个 ClientHello 都没出现。
    ConnectionEnded,
}

impl NoHelloReason {
    pub fn as_code(&self) -> &'static str {
        match self {
            NoHelloReason::FirstHandshakeNotClientHello { .. } => {
                "first_handshake_not_client_hello"
            }
            NoHelloReason::RecordInterleaved { .. } => "record_interleaved",
            NoHelloReason::EncryptedDataWithoutHandshake => "encrypted_data_without_handshake",
            NoHelloReason::ConnectionEnded => "connection_ended",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Phase {
    /// 尚未看到第一条握手消息。
    AwaitHello,
    /// 已成功观察到 ClientHello（终态）。
    Done,
    /// 确认不会再有明文 ClientHello（终态）。
    NoHello(NoHelloReason),
    /// 硬解析错误（终态）。
    Failed,
}

/// 重组中的握手消息：先凑 4 字节头，再按声明长度凑 body。
#[derive(Debug, Clone)]
enum Pending {
    /// 已收到但不足 4 字节的握手头碎片。
    Header(Vec<u8>),
    /// 握手头已确定，正在收 body。
    Body {
        msg_type: u8,
        total_len: usize,
        body: Vec<u8>,
    },
}

/// 增量、无 I/O 的 TLS 记录观察器。
#[derive(Debug)]
pub struct Observer {
    config: Config,
    phase: Phase,
    /// 尚未凑成完整记录的字节（可能含 0..5 字节头 + 部分 fragment）。
    record_buf: Vec<u8>,
    /// 跨记录重组中的握手消息。
    pending: Option<Pending>,
    /// 观察到的 ClientHello（成功后为 Some）。
    client_hello: Option<ClientHelloInfo>,
    /// 终态错误（Failed 时为 Some）。
    error: Option<ParseError>,
    records: Vec<RecordObservation>,
}

/// 一次 [`Observer::finish`] 的观察结论。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Conclusion {
    /// 观察到明文 ClientHello（装箱以压小结本体的体积）。
    ClientHello(Box<ClientHelloInfo>),
    /// 没观察到（附原因）。
    NoClientHello(NoHelloReason),
}

impl Observer {
    pub fn new(config: Config) -> Self {
        Observer {
            config,
            phase: Phase::AwaitHello,
            record_buf: Vec::new(),
            pending: None,
            client_hello: None,
            error: None,
            records: Vec::new(),
        }
    }

    pub fn is_terminal(&self) -> bool {
        !matches!(self.phase, Phase::AwaitHello)
    }

    pub fn client_hello(&self) -> Option<&ClientHelloInfo> {
        self.client_hello.as_ref()
    }

    pub fn error(&self) -> Option<&ParseError> {
        self.error.as_ref()
    }

    pub fn records(&self) -> &[RecordObservation] {
        &self.records
    }

    pub fn config(&self) -> &Config {
        &self.config
    }

    /// 喂入一批 TCP 字节（任意长度、任意切分边界）。
    ///
    /// 成功时表示这批字节本身被消费完（其内部可能触发终态转换，但没有硬错误）；
    /// 返回 `Err` 表示遇到明确的结构违规，观察器永久进入 Failed。
    /// 注意“记录/握手尚未到齐”不是错误：字节被留在内部缓冲区等待下一次喂入。
    pub fn feed(&mut self, chunk: &[u8]) -> Result<(), ParseError> {
        if matches!(self.phase, Phase::Failed) {
            return Err(self
                .error
                .clone()
                .expect("failed observer carries its error"));
        }

        // 终态（Done/NoHello）：仍解析记录*头*做元数据计数，
        // 但 fragment 一律按不透明字节处理（见 process 的 opaque 模式）。
        self.record_buf.extend_from_slice(chunk);
        self.process().inspect_err(|e| {
            self.phase = Phase::Failed;
            self.error = Some(e.clone());
        })
    }

    /// 从 record_buf 中循环取出完整记录。
    fn process(&mut self) -> Result<(), ParseError> {
        loop {
            if self.record_buf.len() < 5 {
                return Ok(());
            }

            // 终态后的“不透明模式”：只按记录头切分计数，不解释 fragment，
            // 头解析失败/长度异常都不再改变既有结论，停止计数即可。
            let opaque = self.is_terminal();
            let header = match RecordHeader::parse(&self.record_buf[..5]) {
                Ok(h) => h,
                Err(_) if opaque => {
                    self.record_buf.clear();
                    return Ok(());
                }
                Err(e) => return Err(e),
            };

            if !opaque && header.fragment_len as usize > self.config.max_record_fragment {
                // 长度上限（子集约束）：头已完整，超限即可确定，
                // 不必等 fragment 到齐——立即判失败，绝不尝试解析内容。
                return Err(ParseError::RecordTooLarge {
                    len: header.fragment_len as usize,
                    max: self.config.max_record_fragment,
                });
            }
            if opaque && header.fragment_len as usize > self.config.max_record_fragment {
                // 不透明模式不再把超限升级为错误（结论已定），停止计数。
                self.record_buf.clear();
                return Ok(());
            }

            // fragment 未到齐则保留缓冲区，等待下一次喂入。
            let total = 5 + header.fragment_len as usize;
            if self.record_buf.len() < total {
                return Ok(());
            }

            // 取出整条记录，保持后续字节在缓冲区中。
            let fragment = self.record_buf[5..total].to_vec();
            self.record_buf.drain(..total);

            let parsed_as_plaintext = if opaque {
                false
            } else {
                self.handle_record(&header, fragment)?
            };

            self.records.push(RecordObservation {
                index: self.records.len(),
                content_type: header.content_type,
                content_type_name: content_type_name(header.content_type),
                version: header.version,
                fragment_len: header.fragment_len as usize,
                parsed_as_plaintext,
            });
        }
    }

    /// 处理一条 fragment 已完整的记录，返回它是否被当作明文握手参与解析。
    fn handle_record(
        &mut self,
        header: &RecordHeader,
        fragment: Vec<u8>,
    ) -> Result<bool, ParseError> {
        if header.content_type != CONTENT_HANDSHAKE {
            if self.pending.is_none() {
                // 等待阶段：CCS/Alert 非致命，继续等；app_data 意味着密文先行。
                if header.content_type == 23 {
                    self.phase = Phase::NoHello(NoHelloReason::EncryptedDataWithoutHandshake);
                }
                return Ok(false);
            }
            // 正在重组握手消息时插入非握手记录：明文结构被破坏/加密开始。
            // 不继续猜测内容，直接收尾以防把密文误解析为握手。
            self.pending = None;
            self.phase = Phase::NoHello(NoHelloReason::RecordInterleaved {
                content_type: header.content_type,
            });
            return Ok(false);
        }

        self.absorb_handshake_fragment(fragment)?;
        // 只有仍在 AwaitHello 阶段参与重组的 handshake 记录才算“按明文处理”；
        // 若该记录已使状态机进入终态，它仍是明文 ClientHello 的载体。
        Ok(matches!(self.phase, Phase::AwaitHello | Phase::Done))
    }

    /// 把一条 handshake 记录的 fragment 追加进重组器，并消费完整消息。
    fn absorb_handshake_fragment(&mut self, fragment: Vec<u8>) -> Result<(), ParseError> {
        if self.pending.is_none() {
            // 空 handshake 记录且无在途消息：合法空片，无需状态。
            if fragment.is_empty() {
                return Ok(());
            }
            self.pending = Some(Pending::Header(Vec::new()));
        }
        match self.pending.as_mut().expect("pending just ensured") {
            Pending::Header(v) => v.extend_from_slice(&fragment),
            Pending::Body { body, .. } => body.extend_from_slice(&fragment),
        }
        self.pump_handshake()
    }

    /// 反复推进：头碎片 → Body；Body 收齐后判定第一条握手消息（必入终态）。
    fn pump_handshake(&mut self) -> Result<(), ParseError> {
        // 头碎片凑齐 4 字节后升级为 Body。
        if let Some(Pending::Header(v)) = &self.pending {
            if v.len() < 4 {
                return Ok(());
            }
            let (msg_type, total_len, rest) = {
                let v = match &self.pending {
                    Some(Pending::Header(v)) => v,
                    _ => unreachable!(),
                };
                let msg_type = v[0];
                let total_len = u32::from_be_bytes([0, v[1], v[2], v[3]]) as usize;
                (msg_type, total_len, v[4..].to_vec())
            };
            if total_len > self.config.max_handshake_message {
                return Err(ParseError::HandshakeTooLarge {
                    len: total_len,
                    max: self.config.max_handshake_message,
                });
            }
            self.pending = Some(Pending::Body {
                msg_type,
                total_len,
                body: rest,
            });
        }

        let complete = match &self.pending {
            Some(Pending::Body {
                total_len, body, ..
            }) => body.len() >= *total_len,
            _ => false,
        };
        if !complete {
            return Ok(());
        }

        let (msg_type, total_len, body) = match self.pending.take() {
            Some(Pending::Body {
                msg_type,
                total_len,
                body,
            }) => (msg_type, total_len, body),
            _ => unreachable!(),
        };
        let body_bytes = body[..total_len].to_vec();
        // 第一条消息之后（同一记录里可能粘连的后续消息）不再处理：
        // 无论是否 ClientHello，状态机都在此进入终态。

        if msg_type == 1 {
            // 解析失败是硬结构错误：终止，绝不退化成“猜密文”。
            let info = ClientHelloInfo::parse(&body_bytes, &self.config)?;
            self.client_hello = Some(info);
            self.phase = Phase::Done;
        } else {
            self.phase = Phase::NoHello(NoHelloReason::FirstHandshakeNotClientHello {
                handshake_type: msg_type,
            });
        }
        Ok(())
    }

    /// 声明字节流结束（对端半关闭/连接关闭）。
    ///
    /// 若仍有未凑齐的记录或握手消息，返回截断错误；空连接不算错误。
    pub fn finish(&mut self) -> Result<Conclusion, ParseError> {
        if let Some(e) = &self.error {
            return Err(e.clone());
        }
        if let Some(info) = &self.client_hello {
            return Ok(Conclusion::ClientHello(Box::new(info.clone())));
        }
        if let Phase::NoHello(reason) = &self.phase {
            return Ok(Conclusion::NoClientHello(reason.clone()));
        }

        // 仍在 AwaitHello：残留字节即截断。
        if !self.record_buf.is_empty() {
            return Err(ParseError::Truncated { what: "record" });
        }
        match &self.pending {
            Some(Pending::Header(v)) if !v.is_empty() => {
                return Err(ParseError::Truncated {
                    what: "handshake_header",
                });
            }
            Some(Pending::Body {
                total_len, body, ..
            }) if body.len() < *total_len => {
                return Err(ParseError::Truncated {
                    what: "handshake_message",
                });
            }
            _ => {}
        }
        Ok(Conclusion::NoClientHello(NoHelloReason::ConnectionEnded))
    }
}
