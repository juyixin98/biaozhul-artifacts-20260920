/**
 * 一键验收脚本：
 *   1. TypeScript 严格类型检查
 *   2. 全量单元/集成测试
 *   3. 启动真实 HTTP 服务，用真实 HTTP 请求验证三档结论
 *   4. CLI 退出码验证（compatible=0 / incompatible=1 / unknown=2）
 *
 * 任一步失败则以非零码退出。
 * 用法: npm run acceptance
 */

import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

function run(cmd: string, args: string[], opts: { env?: NodeJS.ProcessEnv } = {}): Promise<number> {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, {
      cwd: root,
      stdio: 'inherit',
      env: { ...process.env, ...opts.env },
    });
    p.on('error', reject);
    p.on('exit', (code) => resolve(code ?? -1));
  });
}

async function main(): Promise<void> {
  let step = 0;
  const check = async (label: string, fn: () => Promise<number>): Promise<void> => {
    step++;
    console.log(`\n=== [${step}] ${label} ===`);
    const code = await fn();
    if (code !== 0) {
      console.error(`✗ step ${step} failed (exit ${code})`);
      process.exit(1);
    }
    console.log(`✓ ${label}`);
  };

  await check('TypeScript 严格类型检查 (tsc -p tsconfig.check.json)', () =>
    run('npx', ['tsc', '-p', 'tsconfig.check.json']),
  );

  await check('编译到 dist/ (npm run build)', () => run('npm', ['run', 'build']));

  await check('全量测试 (node --test)', () => run('npm', ['test']));

  // ------- 真实 HTTP 服务验收 -------
  console.log('\n=== 启动编译产物 dist/server.js 进行端到端验收 ===');
  // 随机高端口，避免与残留进程冲突
  const port = String(20000 + Math.floor(Math.random() * 20000));
  const server = spawn('node', ['dist/server.js'], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'inherit'],
    env: { ...process.env, PORT: port, HOST: '127.0.0.1', LOG_LEVEL: 'warn' },
  });
  const base = `http://127.0.0.1:${port}`;
  try {
    // 等待端口就绪（简单可靠的轮询循环）
    const deadline = Date.now() + 15000;
    let ready = false;
    while (Date.now() < deadline) {
      try {
        const r = await fetch(`${base}/healthz`);
        if (r.ok) {
          ready = true;
          break;
        }
      } catch {
        /* 服务尚未就绪，继续等待 */
      }
      await new Promise((r) => setTimeout(r, 200));
    }
    if (!ready) throw new Error('server did not start within 15s');
    console.log('✓ 服务已就绪', base);

    const cases: Array<{ pair: string; expect: string }> = [
      { pair: '01-safe-append', expect: 'compatible' },
      { pair: '03-packed-insert', expect: 'incompatible' },
      { pair: '18-unknown-type', expect: 'unknown' },
    ];
    for (const c of cases) {
      const dir = join(root, 'samples', 'pairs', c.pair);
      const oldL = JSON.parse(readFileSync(join(dir, 'v1.storageLayout.json'), 'utf8'));
      const newL = JSON.parse(readFileSync(join(dir, 'v2.storageLayout.json'), 'utf8'));
      const res = await fetch(`${base}/check`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ old: oldL, new: newL }),
      });
      if (!res.ok) throw new Error(`${c.pair}: HTTP ${res.status}`);
      const body = (await res.json()) as { verdict: string };
      if (body.verdict !== c.expect) {
        throw new Error(`${c.pair}: expected ${c.expect}, got ${body.verdict}`);
      }
      console.log(`✓ POST /check ${c.pair} => ${body.verdict}`);
    }
  } finally {
    server.kill('SIGTERM');
  }

  // ------- CLI 退出码验收（使用编译产物 dist/cli.js）-------
  await check('CLI: 兼容对退出码 0', () =>
    run('node', ['dist/cli.js', 'samples/pairs/01-safe-append/v1.storageLayout.json',
      'samples/pairs/01-safe-append/v2.storageLayout.json']),
  );

  console.log('\n=== CLI: 不兼容对退出码 1（失败即成功）===');
  let code = await run('node', ['dist/cli.js', 'samples/pairs/03-packed-insert/v1.storageLayout.json',
    'samples/pairs/03-packed-insert/v2.storageLayout.json']);
  if (code !== 1) {
    console.error(`✗ expected exit 1, got ${code}`);
    process.exit(1);
  }
  console.log('✓ CLI 返回 1');

  console.log('\n=== CLI: 未知对退出码 2 ===');
  code = await run('node', ['dist/cli.js', 'samples/pairs/18-unknown-type/v1.storageLayout.json',
    'samples/pairs/18-unknown-type/v2.storageLayout.json']);
  if (code !== 2) {
    console.error(`✗ expected exit 2, got ${code}`);
    process.exit(1);
  }
  console.log('✓ CLI 返回 2');

  console.log('\n🎉 全部验收步骤通过');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
