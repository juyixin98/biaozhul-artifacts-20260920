import { describe, expect, it } from 'vitest';
import { DecisionLog, type DecisionRecord } from '../../src/diagnostics/decisionLog.js';
import { redactSecret, sanitizeHeaders } from '../../src/diagnostics/redact.js';

function makeRecord(requestId: string, outcome: DecisionRecord['outcome'] = 'partial'): DecisionRecord {
  return {
    requestId,
    timestamp: new Date().toISOString(),
    method: 'GET',
    path: '/objects/x',
    objectId: 'x',
    objectSize: '100',
    requestHeaders: {},
    outcome,
    statusCode: 206,
    reason: null,
    detail: null,
    multipart: false,
    intervals: [],
    parsedSpecCount: 1,
    satisfiableCount: 1,
    unsatisfiableCount: 0,
    unsatisfiableReasons: [],
    plannedTotalBytes: '10',
    actualBodyBytes: '10',
    contentLengthHeader: '10',
    headerBodyConsistent: true,
  };
}

describe('redact', () => {
  it('短串整体掩码，长串保留首尾', () => {
    expect(redactSecret('abc')).toBe('***');
    expect(redactSecret('Bearer abcdef1234567890')).toMatch(/^[A-Za-z]{3}\*\*\*\d{2}$/);
    expect(redactSecret('Bearer abcdef1234567890')).not.toContain('abcdef');
  });

  it('sanitizeHeaders 对敏感头脱敏、普通头原样保留', () => {
    const out = sanitizeHeaders({
      authorization: 'Bearer supersecrettoken',
      cookie: 'session=xyz',
      range: 'bytes=0-9',
      'x-api-key': 'k'.repeat(32),
    });
    expect(out['authorization']).not.toContain('supersecrettoken');
    expect(out['authorization']).toContain('***');
    // 'session=xyz'（11 字符）走首尾掩码，原值片段不得出现
    expect(out['cookie']).not.toContain('session');
    expect(out['cookie']).toContain('***');
    expect(out['x-api-key']).toContain('***');
    expect(out['range']).toBe('bytes=0-9');
  });

  it('数组头合并为逗号串', () => {
    const out = sanitizeHeaders({ 'x-multi': ['a', 'b'] });
    expect(out['x-multi']).toBe('a, b');
  });
});

describe('DecisionLog 环形缓冲', () => {
  it('按 requestId 可查，列表新到旧排序', () => {
    const log = new DecisionLog(3);
    log.record(makeRecord('r1'));
    log.record(makeRecord('r2'));
    log.record(makeRecord('r3'));
    expect(log.get('r2')?.requestId).toBe('r2');
    expect(log.list().map((r) => r.requestId)).toEqual(['r3', 'r2', 'r1']);
  });

  it('容量满后覆盖最旧记录', () => {
    const log = new DecisionLog(2);
    log.record(makeRecord('r1'));
    log.record(makeRecord('r2'));
    log.record(makeRecord('r3'));
    expect(log.size).toBe(2);
    expect(log.get('r1')).toBeNull();
    expect(log.list().map((r) => r.requestId)).toEqual(['r3', 'r2']);
  });

  it('list 支持 limit 且不超过容量', () => {
    const log = new DecisionLog(10);
    for (let i = 0; i < 5; i++) log.record(makeRecord(`r${i}`));
    expect(log.list({ limit: 2 })).toHaveLength(2);
    expect(log.list({ limit: 100 })).toHaveLength(5);
  });

  it('容量必须为正整数', () => {
    expect(() => new DecisionLog(0)).toThrow();
  });
});
