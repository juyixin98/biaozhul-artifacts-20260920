#!/usr/bin/env node
/**
 * CLI: 比较两份 solc storageLayout JSON 文件
 *
 * 用法:
 *   tsx src/cli.ts old.json new.json [--gap '^__gap']
 *   node dist/cli.js old.json new.json
 *
 * 退出码: 0 compatible | 1 incompatible | 2 unknown | 64 用法/输入错误
 */

import { readFileSync } from 'node:fs';
import { compareLayouts } from './compare.js';
import type { SolcStorageLayout } from './types.js';

interface CliArgs {
  oldPath?: string;
  newPath?: string;
  gapPattern?: RegExp;
  json: boolean;
}

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = { json: false };
  const rest: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (a === '--gap') {
      args.gapPattern = new RegExp(argv[++i]!);
    } else if (a === '--json') {
      args.json = true;
    } else if (a === '-h' || a === '--help') {
      printHelp();
      process.exit(0);
    } else {
      rest.push(a);
    }
  }
  args.oldPath = rest[0];
  args.newPath = rest[1];
  return args;
}

function printHelp(): void {
  process.stdout.write(
    [
      'Usage: storage-layout-compat <old-layout.json> <new-layout.json> [options]',
      '',
      'Options:',
      '  --gap <regex>   Regex matching reserve-gap variable names (default: ^__gap(?:_[A-Za-z0-9]+)*$)',
      '  --json          Emit the full JSON report instead of text',
      '  -h, --help      Show this help',
      '',
      'Exit codes: 0 compatible | 1 incompatible | 2 unknown | 64 input error',
      '',
    ].join('\n'),
  );
}

function loadLayout(path: string): SolcStorageLayout {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (err) {
    throw new Error(`cannot read ${path}: ${(err as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`invalid JSON in ${path}: ${(err as Error).message}`);
  }
  const candidate =
    parsed && typeof parsed === 'object' && 'storageLayout' in parsed
      ? (parsed as { storageLayout: unknown }).storageLayout
      : parsed;
  if (
    !candidate ||
    typeof candidate !== 'object' ||
    !Array.isArray((candidate as SolcStorageLayout).storage)
  ) {
    throw new Error(`${path}: not a storageLayout object (expected .storage array)`);
  }
  return candidate as SolcStorageLayout;
}

function formatText(report: ReturnType<typeof compareLayouts>): string {
  const lines: string[] = [];
  lines.push(`verdict: ${report.verdict.toUpperCase()}`);
  lines.push(
    `findings: ${report.summary.totalFindings} ` +
      `(errors=${report.summary.errors}, warnings=${report.summary.warnings}, ` +
      `infos=${report.summary.infos}, unknown=${report.summary.unknownFindings})`,
  );
  lines.push('');
  for (const f of report.findings) {
    lines.push(`[${f.severity.toUpperCase()}] ${f.kind}  @ ${f.path.join(' > ')}`);
    lines.push(`  ${f.message}`);
    if (f.oldEvidence) lines.push(`  old: ${JSON.stringify(f.oldEvidence)}`);
    if (f.newEvidence) lines.push(`  new: ${JSON.stringify(f.newEvidence)}`);
  }
  return lines.join('\n');
}

function main(): void {
  const args = parseArgs(process.argv.slice(2));
  if (!args.oldPath || !args.newPath) {
    printHelp();
    process.exit(64);
  }
  try {
    const oldLayout = loadLayout(args.oldPath);
    const newLayout = loadLayout(args.newPath);
    const report = compareLayouts(
      oldLayout,
      newLayout,
      args.gapPattern ? { gapNamePattern: args.gapPattern } : {},
    );
    process.stdout.write(args.json ? JSON.stringify(report, null, 2) + '\n' : formatText(report) + '\n');
    process.exit(report.verdict === 'compatible' ? 0 : report.verdict === 'incompatible' ? 1 : 2);
  } catch (err) {
    process.stderr.write(`error: ${(err as Error).message}\n`);
    process.exit(64);
  }
}

main();
