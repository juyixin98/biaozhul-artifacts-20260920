//! 手写的 `multipart/form-data` Content-Type 与 part 头解析。
//! 仅实现本项目声明支持的子集，拒绝而非宽容畸形输入。

use crate::error::Error;

/// RFC 2046 bchar：可见 ASCII（0x21..=0x7E），允许 `"`/`(`/`)`/`,`/空格 等之外的常见字符。
///
/// RFC 原文 bchar 集合包含空格与一组特殊字符；为了减少歧义，本实现**不允许空格**，
/// 允许除空格、DEL 及控制字符外的全部 0x21..=0x7E 可见字符——这是 RFC 集合的子集，
/// 主流客户端（curl/浏览器/form 库）生成的 boundary 全部落在其中。
fn is_bchar(b: u8) -> bool {
    matches!(b, 0x21..=0x7e)
}

/// 校验 boundary 原始值（不含前导 `--`）：长度 1..=70 且全部为 bchar，不能全是 `-`。
pub fn validate_boundary(b: &[u8]) -> Result<(), Error> {
    let n = b.len();
    if !(1..=70).contains(&n) {
        return Err(Error::InvalidBoundary);
    }
    if !b.iter().all(|&c| is_bchar(c)) {
        return Err(Error::InvalidBoundary);
    }
    // 全 '-' 与结束标记组合会产生歧义，直接拒绝。
    if b.iter().all(|&c| c == b'-') {
        return Err(Error::InvalidBoundary);
    }
    Ok(())
}

/// 在 `Content-Type` 头的值中解析 `multipart/form-data; boundary=...`。
///
/// 成功返回去引号后的 boundary 字节。允许参数周围的 OWS；类型对子类型大小写不敏感。
pub fn parse_multipart_content_type(value: &str) -> Result<Vec<u8>, Error> {
    let bytes = value.as_bytes();
    let semi = bytes
        .iter()
        .position(|&c| c == b';')
        .ok_or(Error::InvalidBoundary)?;
    let (ty, rest) = value.split_at(semi);
    if !ty.trim().eq_ignore_ascii_case("multipart/form-data") {
        return Err(Error::InvalidBoundary);
    }
    for param in rest[1..].split(';') {
        let eq = param.bytes().position(|c| c == b'=');
        let (name, val) = match eq {
            Some(i) => (param[..i].trim(), param[i + 1..].trim()),
            None => continue, // 忽略无法识别的其他参数（如 charset）
        };
        if name.eq_ignore_ascii_case("boundary") {
            return parse_parameter_value(val);
        }
    }
    Err(Error::InvalidBoundary)
}

/// 解析一个参数值：token 或 quoted-string（支持反斜杠 quoted-pair）。
/// 返回**反转义后**的原始字节（可包含非 UTF-8 字节吗？本实现要求 token 为 ASCII，
/// quoted-string 内允许 obs-text（>=0x80）原样透传）。
fn parse_parameter_value(val: &str) -> Result<Vec<u8>, Error> {
    let v = val.as_bytes();
    if v.is_empty() {
        return Err(Error::InvalidBoundary);
    }
    if v[0] != b'"' {
        // token：1*<除分隔符与空白/控制字符外的 ASCII>
        if !v
            .iter()
            .all(|&c| matches!(c, 0x21..=0x7e) && !b"()<>@,;:\\\"/[]?={}".contains(&c))
        {
            return Err(Error::InvalidBoundary);
        }
        return Ok(v.to_vec());
    }
    // quoted-string
    let mut out = Vec::new();
    let mut i = 1;
    let mut closed = false;
    while i < v.len() {
        match v[i] {
            b'"' => {
                closed = true;
                i += 1;
                break;
            }
            b'\\' => {
                // quoted-pair: 后一个字符原样（仅允许 HT/可见 ASCII/obs-text）
                let &q = v.get(i + 1).ok_or(Error::InvalidBoundary)?;
                if q == b'\r' || q == b'\n' || q == 0x7f || q < 0x20 && q != b'\t' {
                    return Err(Error::InvalidBoundary);
                }
                out.push(q);
                i += 2;
            }
            b'\r' | b'\n' => return Err(Error::InvalidBoundary),
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    if !closed {
        return Err(Error::InvalidBoundary);
    }
    // 闭合引号后只允许空白
    if !v[i..].iter().all(|&c| c == b' ' || c == b'\t') {
        return Err(Error::InvalidBoundary);
    }
    Ok(out)
}

/// 判断字节是否为合法的 header **字段名** token 字符（RFC 7230 tchar 的可见部分）。
fn is_token_char(b: u8) -> bool {
    matches!(b,
        b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.'
        | b'^' | b'_' | b'`' | b'|' | b'~'
        | b'0'..=b'9' | b'a'..=b'z' | b'A'..=b'Z')
}

/// 一个 part 头块的解析结果：`name`、可选 `filename`、其余头（小写名 → 值）。
pub type ParsedHeaders = (String, Option<String>, Vec<(String, String)>);

/// 解析一个 part 的完整头块（不含结尾的 CRLF）。
///
/// 返回 `(PartMeta 需要的信息, 其余头)`：`name`、可选 `filename`，以及小写名的头 map。
/// 必须存在 `Content-Disposition: form-data; name="..."`，否则报错。
pub fn parse_part_headers(block: &[u8]) -> Result<ParsedHeaders, Error> {
    let text = std::str::from_utf8(block).map_err(|_| Error::MalformedHeaders)?;
    let mut name: Option<String> = None;
    let mut filename: Option<String> = None;
    let mut extras: Vec<(String, String)> = Vec::new();

    for raw_line in text.split("\r\n") {
        if raw_line.is_empty() {
            return Err(Error::MalformedHeaders); // 不应出现：调用方已去掉结尾 CRLF
        }
        // 拒绝 obsolete line folding（以空格/制表符续行）。
        if raw_line.starts_with(' ') || raw_line.starts_with('\t') {
            return Err(Error::MalformedHeaders);
        }
        let colon = raw_line
            .bytes()
            .position(|c| c == b':')
            .ok_or(Error::MalformedHeaders)?;
        let (fname, fval) = raw_line.split_at(colon);
        let fval = &fval[1..];
        if !fname.bytes().all(is_token_char) {
            return Err(Error::MalformedHeaders);
        }
        // field-value：可见 ASCII / obs-text / HT，不允许裸 CR/LF（split 已保证）。
        if fval.bytes().any(|c| c < 0x20 && c != b'\t' || c == 0x7f) {
            return Err(Error::MalformedHeaders);
        }
        let fname_l = fname.to_ascii_lowercase();
        let fval_t = fval
            .trim_matches(|c: char| c == ' ' || c == '\t')
            .to_string();

        if fname_l == "content-disposition" {
            let (n, fn_) = parse_disposition(&fval_t)?;
            name = Some(n);
            filename = fn_;
        } else {
            extras.push((fname_l, fval_t));
        }
    }

    match name {
        Some(n) => Ok((n, filename, extras)),
        None => Err(Error::MissingDisposition),
    }
}

/// 解析 `form-data; name="x"; filename="y"`。
fn parse_disposition(value: &str) -> Result<(String, Option<String>), Error> {
    let first_semi = value.bytes().position(|c| c == b';');
    let (disp, rest) = match first_semi {
        Some(i) => (&value[..i], &value[i + 1..]),
        None => (value, ""),
    };
    if !disp.trim().eq_ignore_ascii_case("form-data") {
        return Err(Error::MissingDisposition);
    }

    let mut name: Option<String> = None;
    let mut filename: Option<String> = None;
    for param in rest.split(';') {
        if param.trim().is_empty() {
            continue;
        }
        let eq = param
            .bytes()
            .position(|c| c == b'=')
            .ok_or(Error::MalformedHeaders)?;
        let (pname, pval) = (param[..eq].trim(), param[eq + 1..].trim());
        let decoded = parse_parameter_value(pval).map_err(|_| Error::MalformedHeaders)?;
        if pname.eq_ignore_ascii_case("name") {
            if name.is_some() {
                return Err(Error::MalformedHeaders);
            }
            name = Some(String::from_utf8(decoded).map_err(|_| Error::MalformedHeaders)?);
        } else if pname.eq_ignore_ascii_case("filename") {
            if filename.is_some() {
                return Err(Error::MalformedHeaders);
            }
            filename = Some(String::from_utf8(decoded).map_err(|_| Error::MalformedHeaders)?);
        }
        // 其余参数（filename* 等）忽略
    }

    match name {
        Some(n) => Ok((n, filename)),
        None => Err(Error::MissingDisposition),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boundary_rules() {
        assert!(validate_boundary(b"----WebKitFormBoundaryABC123").is_ok());
        assert!(validate_boundary(b"").is_err());
        assert!(validate_boundary(b"-").is_err()); // 全 '-'
        assert!(validate_boundary(b"with space").is_err());
        assert!(validate_boundary(b"a\nb").is_err());
        let long = vec![b'a'; 71];
        assert!(validate_boundary(&long).is_err());
        let ok70 = vec![b'a'; 70];
        assert!(validate_boundary(&ok70).is_ok());
    }

    #[test]
    fn content_type_variants() {
        assert_eq!(
            parse_multipart_content_type("multipart/form-data; boundary=X").unwrap(),
            b"X"
        );
        assert_eq!(
            parse_multipart_content_type("multipart/form-data; boundary=\"abc 123\"").unwrap(),
            b"abc 123"
        );
        assert_eq!(
            parse_multipart_content_type("Multipart/Form-Data; charset=utf-8; Boundary=zz")
                .unwrap(),
            b"zz"
        );
        assert!(parse_multipart_content_type("multipart/mixed; boundary=x").is_err());
        assert!(parse_multipart_content_type("multipart/form-data").is_err());
        assert!(parse_multipart_content_type("multipart/form-data; boundary=\"x").is_err());
    }

    #[test]
    fn disposition_variants() {
        let (n, f) = parse_disposition("form-data; name=\"a\"").unwrap();
        assert_eq!(n, "a");
        assert!(f.is_none());
        let (n, f) = parse_disposition("form-data; name=up; filename=\"a b\\\".bin\"").unwrap();
        assert_eq!(n, "up");
        assert_eq!(f.unwrap(), "a b\".bin");
        assert!(parse_disposition("attachment; name=\"a\"").is_err());
        assert!(parse_disposition("form-data").is_err());
        assert!(parse_disposition("form-data; name=").is_err());
    }
}
