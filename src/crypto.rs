use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// 签名域：运动命令与急停命令使用不同的域前缀，防止跨接口重放。
const DOMAIN_MOTION: &[u8] = b"motion-command-v1\n";
const DOMAIN_ESTOP: &[u8] = b"estop-command-v1\n";

#[derive(Debug, Clone, Copy)]
pub enum SigDomain {
    Motion,
    Estop,
}

impl SigDomain {
    fn prefix(self) -> &'static [u8] {
        match self {
            SigDomain::Motion => DOMAIN_MOTION,
            SigDomain::Estop => DOMAIN_ESTOP,
        }
    }
}

/// 对“域前缀 + 原始请求体字节”计算 HMAC-SHA256，返回小写十六进制。
/// 签名覆盖的是 HTTP body 原样字节，因此客户端按实际发送的 JSON 字节签名即可。
pub fn sign_hex(key: &[u8], domain: SigDomain, body: &[u8]) -> String {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts key of any length");
    mac.update(domain.prefix());
    mac.update(body);
    hex::encode(mac.finalize().into_bytes())
}

/// 签名校验失败的原因（不区分细节，避免给伪造者提供可利用的区分信息）。
#[derive(Debug, Clone, Copy)]
pub struct SigVerifyError;

/// 解析 `X-Signature: sha256=<hex>`，做常量时间比较。
pub fn verify_header(
    key: &[u8],
    domain: SigDomain,
    body: &[u8],
    header: Option<&str>,
) -> Result<(), SigVerifyError> {
    let provided = header
        .ok_or(SigVerifyError)?
        .strip_prefix("sha256=")
        .ok_or(SigVerifyError)?;
    let provided = hex::decode(provided.trim()).map_err(|_| SigVerifyError)?;

    let mut mac = HmacSha256::new_from_slice(key).map_err(|_| SigVerifyError)?;
    mac.update(domain.prefix());
    mac.update(body);
    // verify_slice 内部对 tag 做常量时间比较。
    mac.verify_slice(&provided).map_err(|_| SigVerifyError)
}

/// 供 `keygen` 子命令：生成 32 字节随机密钥（来自操作系统 CSPRNG）。
pub fn generate_key_hex() -> String {
    let mut buf = [0u8; 32];
    fill_random(&mut buf);
    hex::encode(buf)
}

fn fill_random(buf: &mut [u8]) {
    use std::fs::File;
    use std::io::Read;
    let mut f = File::open("/dev/urandom").expect("打开 /dev/urandom");
    f.read_exact(buf).expect("从 /dev/urandom 读取随机数");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_then_verify_roundtrip() {
        let key = b"0123456789abcdef0123456789abcdef";
        let body = br#"{"vx":1.0}"#;
        let sig = sign_hex(key, SigDomain::Motion, body);
        assert!(
            verify_header(key, SigDomain::Motion, body, Some(&format!("sha256={sig}"))).is_ok()
        );
        // 篡改 body 必须失败
        assert!(verify_header(
            key,
            SigDomain::Motion,
            b"tampered",
            Some(&format!("sha256={sig}"))
        )
        .is_err());
        // 跨域签名必须失败
        assert!(
            verify_header(key, SigDomain::Estop, body, Some(&format!("sha256={sig}"))).is_err()
        );
        // 错密钥必须失败
        assert!(verify_header(
            b"another-32-byte-key-another-32-b",
            SigDomain::Motion,
            body,
            Some(&format!("sha256={sig}"))
        )
        .is_err());
        // 缺头/格式错误
        assert!(verify_header(key, SigDomain::Motion, body, None).is_err());
        assert!(verify_header(key, SigDomain::Motion, body, Some("plainhex")).is_err());
        assert!(verify_header(key, SigDomain::Motion, body, Some("sha256=zz")).is_err());
    }
}
