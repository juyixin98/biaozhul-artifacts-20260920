//! `chfc`：规范霍夫曼编解码命令行与 JSON 控制入口。
//!
//! 子命令：
//! - `compress   -i 输入 -o 输出 [--block-size N]`
//! - `decompress -i 输入 -o 输出 [--max-output N] [--max-block N]
//!               [--incomplete-policy reject|allow]`
//! - `json [文件]`：从文件或标准输入读取一条 JSON 请求，响应写到标准输出
//!
//! 压缩 / 解压的统计打印到 stderr，不污染 stdout（JSON 模式除外）。

use canonical_huffman::jsonapi::handle_request;
use canonical_huffman::stream::{
    compress_stream, decompress_stream, IncompletePolicy, Limits, DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_BYTES, DEFAULT_MAX_OUTPUT_BYTES,
};
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let prog = args.first().map(String::as_str).unwrap_or("chfc");
    let (cmd, rest) = args
        .get(1)
        .map(|s| (s.as_str(), &args[2..]))
        .ok_or_else(|| usage(prog))?;

    match cmd {
        "compress" | "c" => cmd_compress(rest),
        "decompress" | "d" => cmd_decompress(rest),
        "json" | "j" => cmd_json(rest),
        "-h" | "--help" | "help" => {
            println!("{USAGE}");
            Ok(())
        }
        "-V" | "--version" | "version" => {
            println!("chfc {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

const USAGE: &str = concat!(
    "chfc ",
    env!("CARGO_PKG_VERSION"),
    " — canonical Huffman codec\n",
    "\n",
    "USAGE:\n",
    "  chfc compress   -i <input> -o <output> [--block-size N]\n",
    "  chfc decompress -i <input> -o <output> [--max-output N] [--max-block N]\n",
    "                  [--incomplete-policy reject|allow]\n",
    "  chfc json [request.json]        # read one JSON request (file or stdin)\n",
);

fn usage(prog: &str) -> String {
    let _ = prog;
    USAGE.to_string()
}

struct Opts {
    input: Option<String>,
    output: Option<String>,
    block_size: Option<usize>,
    max_output: Option<u64>,
    max_block: Option<u64>,
    policy: Option<IncompletePolicy>,
}

fn parse_opts(args: &[String]) -> Result<Opts, String> {
    let mut opts = Opts {
        input: None,
        output: None,
        block_size: None,
        max_output: None,
        max_block: None,
        policy: None,
    };
    let mut i = 0;
    while i < args.len() {
        let (key, val) = if args[i].starts_with("--") && args[i].contains('=') {
            let (k, v) = args[i].split_once('=').unwrap();
            i += 1;
            (k.to_string(), Some(v.to_string()))
        } else if matches!(
            args[i].as_str(),
            "-i" | "-o" | "--block-size" | "--max-output" | "--max-block" | "--incomplete-policy"
        ) {
            let k = args[i].clone();
            let v = args
                .get(i + 1)
                .ok_or_else(|| format!("missing value for {k}"))?
                .clone();
            i += 2;
            (k, Some(v))
        } else {
            return Err(format!("unexpected argument '{}'", args[i]));
        };
        match key.as_str() {
            "-i" => opts.input = val,
            "-o" => opts.output = val,
            "--block-size" => {
                opts.block_size = Some(
                    val.unwrap()
                        .parse()
                        .map_err(|_| "block-size must be a positive integer".to_string())?,
                );
            }
            "--max-output" => {
                opts.max_output = Some(
                    val.unwrap()
                        .parse()
                        .map_err(|_| "max-output must be an integer".to_string())?,
                );
            }
            "--max-block" => {
                opts.max_block = Some(
                    val.unwrap()
                        .parse()
                        .map_err(|_| "max-block must be an integer".to_string())?,
                );
            }
            "--incomplete-policy" => {
                opts.policy = Some(match val.unwrap().as_str() {
                    "reject" => IncompletePolicy::Reject,
                    "allow" => IncompletePolicy::Allow,
                    other => return Err(format!("invalid incomplete policy '{other}'")),
                });
            }
            other => return Err(format!("unknown option '{other}'")),
        }
    }
    Ok(opts)
}

fn open_input(path: &str) -> Result<BufReader<File>, String> {
    File::open(path)
        .map(BufReader::new)
        .map_err(|e| format!("cannot open input '{path}': {e}"))
}

fn open_output(path: &str) -> Result<BufWriter<File>, String> {
    File::create(path)
        .map(BufWriter::new)
        .map_err(|e| format!("cannot create output '{path}': {e}"))
}

fn cmd_compress(args: &[String]) -> Result<(), String> {
    let opts = parse_opts(args)?;
    let input = opts.input.ok_or("missing -i <input>")?;
    let output = opts.output.ok_or("missing -o <output>")?;
    let block_size = opts.block_size.unwrap_or(DEFAULT_BLOCK_SIZE);
    if block_size == 0 {
        return Err("block-size must be >= 1".into());
    }

    let mut r = open_input(&input)?;
    let mut w = open_output(&output)?;
    let stats = compress_stream(&mut r, &mut w, block_size).map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())?;

    eprintln!(
        "compressed {} -> {} bytes in {} block(s), ratio {:.4} ({:.2}% of input)",
        stats.input_bytes,
        stats.output_bytes,
        stats.blocks,
        stats.ratio(),
        stats.ratio() * 100.0
    );
    Ok(())
}

fn cmd_decompress(args: &[String]) -> Result<(), String> {
    let opts = parse_opts(args)?;
    let input = opts.input.ok_or("missing -i <input>")?;
    let output = opts.output.ok_or("missing -o <output>")?;

    let limits = Limits {
        max_block_bytes: opts.max_block.unwrap_or(DEFAULT_MAX_BLOCK_BYTES),
        max_output_bytes: opts.max_output.unwrap_or(DEFAULT_MAX_OUTPUT_BYTES),
    };
    let policy = opts.policy.unwrap_or_default();

    let mut r = open_input(&input)?;
    let mut w = open_output(&output)?;
    let stats = decompress_stream(&mut r, &mut w, &limits, policy).map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())?;

    eprintln!(
        "decompressed {} -> {} bytes from {} block(s)",
        stats.compressed_bytes, stats.output_bytes, stats.blocks
    );
    Ok(())
}

fn cmd_json(args: &[String]) -> Result<(), String> {
    let mut text = String::new();
    match args.first() {
        Some(path) => {
            File::open(path)
                .map_err(|e| format!("cannot open '{path}': {e}"))?
                .read_to_string(&mut text)
                .map_err(|e| e.to_string())?;
        }
        None => {
            std::io::stdin()
                .read_to_string(&mut text)
                .map_err(|e| e.to_string())?;
        }
    }
    let response = handle_request(&text);
    println!("{response}");
    Ok(())
}
