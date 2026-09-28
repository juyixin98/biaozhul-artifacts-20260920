import { describe, expect, it } from 'vitest';
import { parseEnvelope } from '../src/contract/parse.js';
import { ErrorCode } from '../src/contract/protocol.js';

describe('contract parser: parse errors vs business errors are separate layers', () => {
  it('classifies syntactically invalid JSON as PARSE_ERROR (-32700)', () => {
    const result = parseEnvelope('{not json');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe(ErrorCode.PARSE_ERROR);
      expect(result.code).toBe(-32700);
    }
  });

  it('classifies an oversized body (>1 MiB) as PARSE_ERROR, not as an accepted request', () => {
    const oversized = '{"jsonrpc":"2.0","method":"echo","params":{"pad":"' + 'x'.repeat(1_050_000) + '"},"id":1}';
    const result = parseEnvelope(oversized);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe(-32700);
  });

  it('classifies a bare JSON primitive as INVALID_REQUEST (-32600), not parse error', () => {
    const result = parseEnvelope('42');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.topLevel).toBe('single');
      expect(result.messages[0]?.kind).toBe('invalid');
      if (result.messages[0]?.kind === 'invalid') {
        expect(result.messages[0].code).toBe(ErrorCode.INVALID_REQUEST);
        expect(result.messages[0].reason).toBe('invalid-request');
        expect(result.messages[0].id).toBeNull();
      }
    }
  });

  it('distinguishes empty batch [] from a single-element batch', () => {
    const empty = parseEnvelope('[]');
    expect(empty.ok).toBe(true);
    if (empty.ok) {
      expect(empty.topLevel).toBe('batch');
      expect(empty.messages).toHaveLength(0);
    }

    const single = parseEnvelope('[{"jsonrpc":"2.0","method":"ping","id":1}]');
    expect(single.ok).toBe(true);
    if (single.ok) {
      expect(single.topLevel).toBe('batch');
      expect(single.messages).toHaveLength(1);
      expect(single.messages[0]?.kind).toBe('request');
    }
  });

  it('treats a valid JSON string as invalid request, not parse error', () => {
    const result = parseEnvelope('"hello"');
    expect(result.ok).toBe(true);
    if (result.ok && result.messages[0]?.kind === 'invalid') {
      expect(result.messages[0].code).toBe(-32600);
    }
  });

  it('rejects wrong jsonrpc version with -32600', () => {
    const result = parseEnvelope('{"jsonrpc":"1.0","method":"ping","id":1}');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.messages[0]?.kind).toBe('invalid');
    }
  });

  it('distinguishes a notification (no id) from a request with id null', () => {
    const notification = parseEnvelope('{"jsonrpc":"2.0","method":"ping"}');
    expect(notification.ok).toBe(true);
    if (notification.ok) {
      const msg = notification.messages[0];
      expect(msg?.kind).toBe('notification');
      expect(msg?.isNotification).toBe(true);
    }

    const nullId = parseEnvelope('{"jsonrpc":"2.0","method":"ping","id":null}');
    expect(nullId.ok).toBe(true);
    if (nullId.ok) {
      const msg = nullId.messages[0];
      expect(msg?.kind).toBe('request');
      if (msg?.kind === 'request') expect(msg.id).toBeNull();
    }
  });

  it('rejects boolean, object and array ids and echoes null', () => {
    for (const badId of ['true', '{"a":1}', '[1]']) {
      const result = parseEnvelope(`{"jsonrpc":"2.0","method":"ping","id":${badId}}`);
      expect(result.ok).toBe(true);
      if (result.ok) {
        const msg = result.messages[0];
        expect(msg?.kind).toBe('invalid');
        if (msg?.kind === 'invalid') {
          expect(msg.id).toBeNull();
          expect(msg.code).toBe(-32600);
        }
      }
    }
  });

  it('rejects non-safe-integer numeric ids (float, NaN-ish, huge)', () => {
    for (const bad of ['1.5', '1e999']) {
      const result = parseEnvelope(`{"jsonrpc":"2.0","method":"ping","id":${bad}}`);
      if (result.ok) expect(result.messages[0]?.kind).toBe('invalid');
      else expect(result.ok).toBe(true); // 1e999 parses to null -> invalid
    }
  });

  it('rejects non-structured params', () => {
    const result = parseEnvelope('{"jsonrpc":"2.0","method":"ping","params":4,"id":1}');
    if (result.ok) expect(result.messages[0]?.kind).toBe('invalid');
  });

  it('tags invalid elements inside a batch differently from a single invalid request', () => {
    const result = parseEnvelope('[{"jsonrpc":"2.0","method":"ping","id":1}, 5]');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.messages).toHaveLength(2);
      expect(result.messages[0]?.kind).toBe('request');
      expect(result.messages[1]?.kind).toBe('invalid');
      if (result.messages[1]?.kind === 'invalid') {
        expect(result.messages[1].reason).toBe('invalid-batch-element');
      }
    }
  });

  it('treats null as a single invalid value, NOT as an empty/notification batch', () => {
    const result = parseEnvelope('null');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.topLevel).toBe('single');
      expect(result.messages[0]?.kind).toBe('invalid');
    }
  });
});
