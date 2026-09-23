//! 生成 samples/ 目录下的请求样例报文与 samples/README.md（含逐字节 hex 注释）。
//!
//! 运行：
//!     cargo run --example gen_samples
//!
//! 样例分为三组：
//! - 合法：标准查询（A/AAAA/CNAME）、压缩响应（CNAME 指针）、前向指针响应、
//!   未知类型响应、根域名查询；
//! - 畸形：指针自环、指针互环、标签越界、截断资源记录、保留标签前缀、扩展标签前缀；
//! - 压力：70 跳指针链（默认上限 64 时触发 PointerChainTooLong）。

use std::fs;
use std::path::Path;

use dns_compress::message::{Message, CLASS_IN, TYPE_A, TYPE_AAAA, TYPE_CNAME};
use dns_compress::name::Name;
use dns_compress::parser::Limits;

struct Sample {
    file: &'static str,
    title: &'static str,
    expect: &'static str,
    bytes: Vec<u8>,
}

fn main() {
    let dir = Path::new("samples");
    fs::create_dir_all(dir).expect("创建 samples/ 失败");

    let samples = vec![
        query_a(),
        query_aaaa(),
        query_cname(),
        query_root(),
        response_alias_compressed(),
        response_forward_pointer(),
        response_unknown_type(),
        bad_loop_self(),
        bad_loop_pair(),
        bad_label_overrun(),
        bad_truncated_rr(),
        bad_reserved_label(),
        bad_extended_label(),
        stress_pointer_chain(),
    ];

    let mut md = String::new();
    md.push_str("# 请求样例\n\n");
    md.push_str("由 `cargo run --example gen_samples` 生成。每行为报文的十六进制转储。\n\n");
    md.push_str("配合服务测试：`./scripts/demo.sh`，或手动：\n\n");
    md.push_str("```sh\ncargo run --release --bin dns-tcp-server &\n");
    md.push_str("cargo run --example client -- 127.0.0.1:10053 samples/query_a.bin\n```\n\n");
    md.push_str("| 文件 | 说明 | 预期 |\n|---|---|---|\n");
    for s in &samples {
        md.push_str(&format!("| `{}` | {} | {} |\n", s.file, s.title, s.expect));
    }
    md.push('\n');
    for s in &samples {
        md.push_str(&format!(
            "## {}\n\n{}\n\n预期：{}\n\n```\n",
            s.file, s.title, s.expect
        ));
        md.push_str(&hex_dump(&s.bytes));
        md.push_str("```\n\n");
        fs::write(dir.join(s.file), &s.bytes).expect("写样例失败");
    }
    fs::write(dir.join("README.md"), md).expect("写 samples/README.md 失败");
    println!("已生成 {} 个样例到 samples/", samples.len());

    // 自检：合法样例必须能解析。
    let limits = Limits::default();
    for (i, s) in samples.iter().enumerate() {
        if i < 7 {
            Message::parse(&s.bytes, &limits)
                .unwrap_or_else(|e| panic!("合法样例 {} 解析失败：{e}", s.file));
        }
    }
    println!("合法样例自检通过");
}

fn hex_dump(b: &[u8]) -> String {
    let mut out = String::new();
    for (i, x) in b.iter().enumerate() {
        if i % 16 == 0 {
            out.push_str(&format!("{i:04x}: "));
        }
        out.push_str(&format!("{x:02x} "));
        if i % 16 == 15 {
            out.push('\n');
        }
    }
    if !b.len().is_multiple_of(16) {
        out.push('\n');
    }
    out
}

fn header(id: u16, flags: u16, qd: u16, an: u16, ns: u16, ar: u16) -> Vec<u8> {
    let mut v = Vec::with_capacity(12);
    v.extend_from_slice(&id.to_be_bytes());
    v.extend_from_slice(&flags.to_be_bytes());
    v.extend_from_slice(&qd.to_be_bytes());
    v.extend_from_slice(&an.to_be_bytes());
    v.extend_from_slice(&ns.to_be_bytes());
    v.extend_from_slice(&ar.to_be_bytes());
    v
}

fn question(name: &str, qtype: u16) -> Vec<u8> {
    let mut v = Name::from_dotted(name).expect("样例名字合法").encode();
    v.extend_from_slice(&qtype.to_be_bytes());
    v.extend_from_slice(&CLASS_IN.to_be_bytes());
    v
}

fn query_a() -> Sample {
    let mut b = header(0x1234, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&question("www.example.com", TYPE_A));
    Sample {
        file: "query_a.bin",
        title: "标准 A 查询（无压缩）",
        expect: "NOERROR，回答 192.0.2.1",
        bytes: b,
    }
}

fn query_aaaa() -> Sample {
    let mut b = header(0x1235, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&question("www.example.com", TYPE_AAAA));
    Sample {
        file: "query_aaaa.bin",
        title: "标准 AAAA 查询",
        expect: "NOERROR，回答 2001:db8::1",
        bytes: b,
    }
}

fn query_cname() -> Sample {
    let mut b = header(0x1236, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&question("www.example.com", TYPE_CNAME));
    Sample {
        file: "query_cname.bin",
        title: "标准 CNAME 查询",
        expect: "NOERROR，回答 alias.example.com.",
        bytes: b,
    }
}

fn query_root() -> Sample {
    let mut b = header(0x1237, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&question(".", TYPE_A));
    Sample {
        file: "query_root.bin",
        title: "根域名（.）A 查询",
        expect: "NOERROR，回答 192.0.2.1",
        bytes: b,
    }
}

/// 压缩响应：www.example.com 的 CNAME 记录，名字与 RDATA 目标都用回指指针。
fn response_alias_compressed() -> Sample {
    let mut b = header(0x9abc, 0x8180, 1, 1, 0, 0);
    let q = question("www.example.com", TYPE_CNAME);
    b.extend_from_slice(&q);
    // 问题名 "www.example.com" 起始于 12；"example.com" 起始于 12+1+3=16。
    assert_eq!(b.len(), 12 + q.len());
    let name_off = 12u16;
    let suffix_off = 16u16;
    // RR：NAME=指针→12，TYPE=CNAME，CLASS=IN，TTL=300
    b.extend_from_slice(&(0xC000u16 | name_off).to_be_bytes());
    b.extend_from_slice(&TYPE_CNAME.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&300u32.to_be_bytes());
    // RDATA：标签 "alias" + 指针→16（example.com）
    let rdlen = 1 + 5 + 2;
    b.extend_from_slice(&(rdlen as u16).to_be_bytes());
    b.push(5);
    b.extend_from_slice(b"alias");
    b.extend_from_slice(&(0xC000u16 | suffix_off).to_be_bytes());
    Sample {
        file: "response_alias_compressed.bin",
        title: "压缩响应：CNAME 记录，名字与目标均用回指指针",
        expect: "QR=1 回显：解码后重新编码（非压缩），语义不变",
        bytes: b,
    }
}

/// 前向指针：问题的 QNAME 末尾不是根零字节，而是指向后面 RDATA 中名字的指针。
/// 报文形态：对一个 A 查询返回 CNAME（CNAME 的 RDATA 是被指向的名字）。
fn response_forward_pointer() -> Sample {
    let mut b = header(0xf00d, 0x8180, 1, 1, 0, 0);
    // QNAME = "www" + 前向指针（占位，稍后回填）
    b.push(3);
    b.extend_from_slice(b"www");
    let fwd_pos = b.len();
    b.extend_from_slice(&[0, 0]); // 指针占位
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    // RR：NAME=指针→12（"www" 处），TYPE=CNAME，CLASS=IN，TTL=60
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());
    b.extend_from_slice(&TYPE_CNAME.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    // RDATA：名字 "example.com"（前向指针的目标）
    let rdata_target = b.len() + 2; // RDLENGTH 之后的 RDATA 起始
    let rdlen = 1 + 7 + 1 + 3 + 1; // example.com 的在线长度
    b.extend_from_slice(&(rdlen as u16).to_be_bytes());
    assert_eq!(b.len(), rdata_target);
    b.extend_from_slice(&Name::from_dotted("example.com").unwrap().encode());
    // 回填前向指针
    let ptr = 0xC000u16 | (rdata_target as u16);
    b[fwd_pos] = (ptr >> 8) as u8;
    b[fwd_pos + 1] = (ptr & 0xFF) as u8;
    Sample {
        file: "response_forward_pointer.bin",
        title: "前向指针：QNAME 指向其后 RDATA 中的名字（合法但不常见）",
        expect: "解码成功：QNAME=www.example.com.，CNAME=example.com.；回显为语义等价的非压缩报文",
        bytes: b,
    }
}

/// 未知类型（TYPE99/SPF）响应：RDATA 原样保留。
fn response_unknown_type() -> Sample {
    let mut b = header(0x7777, 0x8180, 1, 1, 0, 0);
    b.extend_from_slice(&question("example.com", 99));
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // NAME=指针→12
    b.extend_from_slice(&99u16.to_be_bytes()); // TYPE=99
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&300u32.to_be_bytes());
    let rdata = b"v=spf1 -all";
    b.extend_from_slice(&(rdata.len() as u16).to_be_bytes());
    b.extend_from_slice(rdata);
    Sample {
        file: "response_unknown_type.bin",
        title: "未知类型（TYPE99）响应：RDATA 原样保留字节",
        expect: "QR=1 回显：RDATA 字节逐字节一致",
        bytes: b,
    }
}

/// 指针自环：QNAME 是一个指向自身偏移的指针。
fn bad_loop_self() -> Sample {
    let mut b = header(0xbad1, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // 指向偏移 12（自身）
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    Sample {
        file: "bad_loop_self.bin",
        title: "畸形：QNAME 指针指向自身（自环）",
        expect: "FORMERR（PointerLoop）",
        bytes: b,
    }
}

/// 指针互环：偏移 12 的指针指向 14，偏移 14 的指针指回 12。
fn bad_loop_pair() -> Sample {
    let mut b = header(0xbad2, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&0xC00Eu16.to_be_bytes()); // 12: 指向 14
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // 14: 指回 12
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    Sample {
        file: "bad_loop_pair.bin",
        title: "畸形：两个指针互相指向（互环）",
        expect: "FORMERR（PointerLoop）",
        bytes: b,
    }
}

/// 标签越界：标签声明 10 字节，但报文到此结束。
fn bad_label_overrun() -> Sample {
    let mut b = header(0xbad3, 0x0100, 1, 0, 0, 0);
    b.push(10); // 声明 10 字节标签
    b.extend_from_slice(b"ab"); // 实际只有 2 字节
    Sample {
        file: "bad_label_overrun.bin",
        title: "畸形：标签长度越过报文末尾",
        expect: "FORMERR（UnexpectedEof）",
        bytes: b,
    }
}

/// 截断的资源记录：RDLENGTH=8，实际只剩 4 字节。
fn bad_truncated_rr() -> Sample {
    let mut b = header(0xbad4, 0x8180, 1, 1, 0, 0);
    b.extend_from_slice(&question("example.com", TYPE_A));
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&8u16.to_be_bytes()); // RDLENGTH=8
    b.extend_from_slice(&[192, 0, 2, 1]); // 实际只有 4 字节
    Sample {
        file: "bad_truncated_rr.bin",
        title: "畸形：RDLENGTH 声明 8 字节但报文只剩 4 字节",
        expect: "FORMERR（RdataLengthMismatch）",
        bytes: b,
    }
}

/// 保留标签前缀 01。
fn bad_reserved_label() -> Sample {
    let mut b = header(0xbad5, 0x0100, 1, 0, 0, 0);
    b.push(0x40); // 高两位 01：RFC 1035 保留
    b.extend_from_slice(b"xx");
    Sample {
        file: "bad_reserved_label.bin",
        title: "畸形：标签使用保留前缀 01",
        expect: "FORMERR（ReservedLabelKind）",
        bytes: b,
    }
}

/// 扩展标签前缀 10（EDNS 扩展标签，本实现不支持）。
fn bad_extended_label() -> Sample {
    let mut b = header(0xbad6, 0x0100, 1, 0, 0, 0);
    b.push(0x80); // 高两位 10：扩展标签
    b.extend_from_slice(b"xx");
    Sample {
        file: "bad_extended_label.bin",
        title: "畸形：标签使用扩展前缀 10（EDNS 扩展标签）",
        expect: "FORMERR（UnsupportedExtendedLabel）",
        bytes: b,
    }
}

/// 71 跳指针链：RR 在最前（属主名是指向链尾的指针），70 个槽位依次回指，
/// 链长超过默认上限 64。
fn stress_pointer_chain() -> Sample {
    let mut b = header(0xbeef, 0x8180, 0, 1, 0, 0);
    // RR 必须排在链槽之前：解析器从偏移 12 开始读 RR。
    let name_ptr_pos = b.len();
    b.extend_from_slice(&[0, 0]); // 属主名指针占位（指向最深槽位）
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&4u16.to_be_bytes());
    b.extend_from_slice(&[192, 0, 2, 1]);

    let chain_start = b.len();
    // 70 个指针槽位：slot i（偏移 chain_start+2i）指向 slot i-1；slot0 指向链后的根。
    for i in 0..70u16 {
        let target = if i == 0 {
            chain_start + 70 * 2
        } else {
            chain_start + 2 * (i - 1) as usize
        };
        b.extend_from_slice(&(0xC000u16 | target as u16).to_be_bytes());
    }
    b.push(0); // 根零字节（链的终点）
    let deepest = chain_start + 2 * 69;
    let ptr = 0xC000u16 | deepest as u16;
    b[name_ptr_pos] = (ptr >> 8) as u8;
    b[name_ptr_pos + 1] = (ptr & 0xFF) as u8;
    Sample {
        file: "stress_pointer_chain.bin",
        title: "压力：71 跳指针链（默认上限 64）",
        expect: "FORMERR（PointerChainTooLong）；上限调到 128 可正常解码",
        bytes: b,
    }
}
