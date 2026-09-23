//! gen-samples：生成合成报文样例到 samples/ 目录。
//!
//! 每个样例是“按 TCP 发送顺序排列的裸字节”，可直接发给 tls-observer-server。
//! 同时生成 samples/MANIFEST.txt 描述每个文件与预期结论。
//!
//! 用法：gen-samples [输出目录，默认 samples]

use std::fs;
use std::path::PathBuf;

use tls_observer::record::{
    CONTENT_ALERT, CONTENT_APPLICATION_DATA, CONTENT_CHANGE_CIPHER_SPEC, CONTENT_HANDSHAKE,
};
use tls_observer::test_support::*;

struct Sample {
    name: &'static str,
    description: &'static str,
    expected: &'static str,
    data: Vec<u8>,
}

fn main() {
    let out_dir: PathBuf = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("samples"));
    fs::create_dir_all(&out_dir).expect("create samples dir");

    let samples = build_samples();
    let mut manifest = String::new();
    manifest.push_str("# TLS 记录观察器 —— 合成报文样例清单\n");
    manifest.push_str("# 每个文件即按 TCP 发送顺序排列的裸字节，直接发给 server 即可复现。\n\n");

    for s in &samples {
        let path = out_dir.join(format!("{}.bin", s.name));
        fs::write(&path, &s.data).expect("write sample");
        manifest.push_str(&format!(
            "== {} ({})\n  报文: {}\n  预期: {}\n  字节: {}\n\n",
            s.name,
            path.display(),
            s.description,
            s.expected,
            s.data.len()
        ));
        println!(
            "wrote {} ({} bytes) — expected: {}",
            path.display(),
            s.data.len(),
            s.expected
        );
    }

    fs::write(out_dir.join("MANIFEST.txt"), manifest).expect("write manifest");
    println!("wrote {}", out_dir.join("MANIFEST.txt").display());
}

fn ch_good() -> Vec<u8> {
    ClientHelloBuilder::new()
        .with_grease_extension(0x2A2A)
        .with_sni("example.com")
        .with_alpn(&["h2", "http/1.1"])
        .with_supported_versions(&[0x2A2A, 0x0304])
        .with_unknown_extension(0x0017, &[0x00, 0x11, 0x22]) // 伪造“未知扩展”
        .build_message()
}

fn build_samples() -> Vec<Sample> {
    let mut samples = Vec::new();

    // 01 正常：单条记录承载完整 ClientHello（含 GREASE/SNI/ALPN/未知扩展）。
    let msg = ch_good();
    samples.push(Sample {
        name: "01_valid_single_record",
        description: "一条 TLS 记录承载完整明文 ClientHello（SNI=example.com, ALPN=h2/http1.1, GREASE, 未知扩展）",
        expected: "result=client_hello, sni=example.com, alpn=[h2,http/1.1]",
        data: record(CONTENT_HANDSHAKE, 0x0301, &msg),
    });

    // 02 跨记录重组：一个握手消息切到 3 条记录，切点跨越握手头与 body。
    let split_bytes = split_across_records(&msg, &[2, 7, 100]);
    samples.push(Sample {
        name: "02_handshake_split_records",
        description: "同一个 ClientHello 被拆进 3 条 handshake 记录（切点跨 4 字节握手头与扩展块）",
        expected: "result=client_hello（跨记录重组成功），sni=example.com",
        data: split_bytes,
    });

    // 03 重复扩展：两个完全相同的非 GREASE 扩展（SNI 出现两次）。
    let dup = ClientHelloBuilder::new()
        .with_sni("a.example")
        .with_sni("b.example")
        .build_message();
    samples.push(Sample {
        name: "03_duplicate_extension",
        description: "扩展块中 server_name(0x0000) 出现两次（非 GREASE 重复扩展）",
        expected: "result=parse_error, DuplicateExtension ext_type=0x0000",
        data: record(CONTENT_HANDSHAKE, 0x0301, &dup),
    });

    // 04 嵌套长度不符（SNI 内层 list 长度大于外层扩展数据）。
    // 手工构造：扩展头说 data 有 5 字节，但其中 list 长度字段写 100。
    let mut bad_sni_data = Vec::new();
    bad_sni_data.extend_from_slice(&100u16.to_be_bytes()); // server_name_list 声明 100
    bad_sni_data.push(0); // name_type
    bad_sni_data.extend_from_slice(&3u16.to_be_bytes());
    bad_sni_data.extend_from_slice(b"abc");
    let bad_ext = extension(0x0000, &bad_sni_data); // 扩展 data 实际只有 8 字节
    let nested = ClientHelloBuilder::new()
        .with_raw_extension(bad_ext)
        .build_message();
    samples.push(Sample {
        name: "04_nested_length_mismatch_sni",
        description: "SNI 扩展内层 server_name_list 声明 100 字节，外层扩展数据仅 8 字节",
        expected: "result=parse_error, LengthMismatch field=server_name_list",
        data: record(CONTENT_HANDSHAKE, 0x0301, &nested),
    });

    // 05 GREASE 丰富场景：多个 GREASE（扩展两个不同槽位 + 套件 + 版本），
    // 验证 GREASE 不触发重复扩展、不算未知。
    let grease = ClientHelloBuilder::new()
        .with_grease_extension(0x8A8A)
        .with_grease_extension(0xCACA)
        .with_sni("grease.example")
        .with_alpn(&["h2"])
        .with_supported_versions(&[0x8A8A, 0x0304, 0x0303])
        .build_message();
    samples.push(Sample {
        name: "05_grease_values",
        description: "GREASE 出现在扩展(两个不同值)/密码套件/supported_versions",
        expected: "result=client_hello；GREASE 被过滤，无重复扩展错误，sni=grease.example",
        data: record(CONTENT_HANDSHAKE, 0x0301, &grease),
    });

    // 06 截断：ClientHello 记录声明的长度大于实际提供的字节（连接中途断）。
    let truncated = {
        let mut r = record(CONTENT_HANDSHAKE, 0x0301, &msg);
        r.truncate(r.len() - 40); // 砍掉末尾 40 字节，但记录头长度不变
        r
    };
    samples.push(Sample {
        name: "06_truncated_record",
        description: "记录头声明的 fragment 长度比实际到齐字节多 40，随后连接结束",
        expected: "result=parse_error, Truncated what=record",
        data: truncated,
    });

    // 07 ClientHello 后紧跟密文（app_data 随机字节），不得把密文当握手。
    let mut after_ch = record(CONTENT_HANDSHAKE, 0x0301, &msg);
    after_ch.extend_from_slice(&record(CONTENT_CHANGE_CIPHER_SPEC, 0x0301, &[0x01]));
    after_ch.extend_from_slice(&record(
        CONTENT_APPLICATION_DATA,
        0x0303,
        &pseudo_random(64, 12345),
    ));
    samples.push(Sample {
        name: "07_ciphertext_after_hello",
        description: "ClientHello 后接 CCS 与 64 字节伪随机 app_data（模拟加密流量）",
        expected: "result=client_hello；app_data 标记为非明文，绝不二次解析",
        data: after_ch,
    });

    // 08 只有“密文”：开头就是 app_data 随机字节（且其内部恰好出现 0x16）。
    let ct_only = record(
        CONTENT_APPLICATION_DATA,
        0x0303,
        &pseudo_random(128, 0xBADF00D),
    );
    samples.push(Sample {
        name: "08_encrypted_only",
        description: "无任何握手记录，直接一条 128 字节 app_data（随机字节中含 0x16/长度样式）",
        expected: "result=no_client_hello, reason=encrypted_data_without_handshake",
        data: ct_only,
    });

    // 09 记录超长：fragment 声明 16385（超过默认上限 16384）。
    samples.push(Sample {
        name: "09_record_too_large",
        description: "记录头声明 fragment=16385，仅给少量实际字节",
        expected: "result=parse_error, RecordTooLarge max=16384",
        data: record_with_declared_len(CONTENT_APPLICATION_DATA, 0x0303, 16385, &[0xAA; 10]),
    });

    // 10 非法 content type（30）。
    samples.push(Sample {
        name: "10_bad_content_type",
        description: "记录头 content_type=30（不在 20/21/22/23 支持子集内）",
        expected: "result=parse_error, BadRecordContentType(30)",
        data: record_with_declared_len(30, 0x0301, 1, &[0x00]),
    });

    // 11 第一条握手消息不是 ClientHello（ServerHello, type=2）。
    let server_hello = build_server_hello_like_body();
    samples.push(Sample {
        name: "11_first_message_server_hello",
        description: "第一条握手消息 HandshakeType=2（ServerHello），后附 app_data",
        expected: "result=no_client_hello, reason=first_handshake_not_client_hello",
        data: {
            let mut d = record(CONTENT_HANDSHAKE, 0x0303, &handshake(2, &server_hello));
            d.extend_from_slice(&record(
                CONTENT_APPLICATION_DATA,
                0x0303,
                &pseudo_random(32, 7),
            ));
            d
        },
    });

    // 12 非法记录层版本（0x0002，SSL 2.0 风格）。
    samples.push(Sample {
        name: "12_bad_record_version",
        description: "记录层 version=0x0002（不在 0x0301..=0x0304 内）",
        expected: "result=parse_error, BadRecordVersion",
        data: record_with_declared_len(CONTENT_HANDSHAKE, 0x0002, 0, &[]),
    });

    // 13 跨“极小 TCP 分片”视角：整个 ClientHello 按每 3 字节切（发送端视角，
    // 文件本身仍是完整字节；server 端可逐 3 字节喂入做增量验证，见集成测试）。
    samples.push(Sample {
        name: "13_valid_for_tiny_tcp_segments",
        description: "完整 ClientHello（可按任意字节边界喂入，测试用每 3 字节一片）",
        expected: "result=client_hello（任意增量切分下结论一致）",
        data: record(CONTENT_HANDSHAKE, 0x0301, &msg),
    });

    // 14 空连接：无任何字节。
    samples.push(Sample {
        name: "14_empty_connection",
        description: "连接建立后立即关闭，无字节",
        expected: "result=no_client_hello, reason=connection_ended",
        data: Vec::new(),
    });

    // 15 握手头都没收全（3 字节后连接结束）。
    samples.push(Sample {
        name: "15_truncated_handshake_header",
        description: "一个 handshake 记录的 fragment 只给了 3 字节（不足 4 字节握手头）即结束",
        expected: "result=parse_error, Truncated what=handshake_header",
        data: record(CONTENT_HANDSHAKE, 0x0301, &[0x01, 0x00, 0x01]),
    });

    // 16 CCS/Alert 先于握手：非致命，随后才是 ClientHello。
    let mut ccs_then_hello = record(CONTENT_CHANGE_CIPHER_SPEC, 0x0301, &[0x01]);
    ccs_then_hello.extend_from_slice(&record(CONTENT_ALERT, 0x0301, &[0x01, 0x00]));
    ccs_then_hello.extend_from_slice(&record(CONTENT_HANDSHAKE, 0x0301, &ch_good()));
    samples.push(Sample {
        name: "16_ccs_alert_then_hello",
        description: "ClientHello 之前先出现 CCS 与 Alert（非致命杂项记录），随后正常 ClientHello",
        expected: "result=client_hello（前两条记录视为非明文杂项）",
        data: ccs_then_hello,
    });

    samples
}

/// 确定性伪随机字节（LCG），保证样例可复现。
fn pseudo_random(n: usize, seed: u64) -> Vec<u8> {
    let mut state = seed.max(1);
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        // 与 glibc rand 风格一致的常数；刻意让部分字节落在 0x16/0x03 附近。
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        out.push((state >> 33) as u8);
    }
    // 确保其中至少出现一个 0x16 与一个看似记录头的模式，增强“别误解析”的覆盖。
    if n >= 5 {
        out[2] = 0x16;
        out[3] = 0x03;
        out[4] = 0x03;
    }
    out
}

/// 构造一个形态合法但类型为 ServerHello 的握手体（仅用于样例，不求完整）。
fn build_server_hello_like_body() -> Vec<u8> {
    let mut b = Vec::new();
    b.extend_from_slice(&0x0303u16.to_be_bytes()); // server_version
    b.extend_from_slice(&[0x22; 32]); // random
    b.push(32); // session_id 长度
    b.extend_from_slice(&[0x33; 32]);
    b.extend_from_slice(&0x1301u16.to_be_bytes()); // cipher_suite
    b.push(0x00); // compression
    b
}
