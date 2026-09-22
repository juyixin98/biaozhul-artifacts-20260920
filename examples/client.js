#!/usr/bin/env node
/* eslint-disable */
/**
 * StageVault demo client (Node 18+).
 *
 * Usage:
 *   # Host: create demo deck, publish, open a session and drive it
 *   node examples/client.js host
 *
 *   # Participant: join an existing session, raise/cancel hand, follow sync
 *   node examples/client.js participant <JOIN_CODE> <USER_ID> [NAME]
 *
 * Env: API_BASE (default http://localhost:3000)
 *
 * The host script is a scripted walk-through; the participant script connects
 * the websocket and logs every live event / snapshot it receives.
 */
const { io } = require('socket.io-client');

const BASE = process.env.API_BASE || 'http://localhost:3000';
const http = async (method, path, body, userId) => {
  const res = await fetch(BASE + path, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(userId ? { 'x-user-id': userId } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(`${method} ${path} -> ${res.status} ${JSON.stringify(json)}`);
    err.status = res.status;
    err.body = json;
    throw err;
  }
  return json;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rid = () => `req-${Math.random().toString(36).slice(2)}`;

function connectSocket(sessionId, userId, lastSeq, onEvent, onSnapshot) {
  const socket = io(BASE, {
    auth: { sessionId, userId, lastSeq },
    transports: ['websocket'],
  });
  socket.on('connected', (msg) => {
    console.log('[ws] connected:', msg.kind, msg.kind === 'snapshot'
      ? `version=${msg.snapshot.version} status=${msg.snapshot.status}`
      : `replayed ${msg.events.length} event(s)`);
  });
  socket.on('event', (e) => onEvent(e));
  socket.on('snapshot', ({ snapshot }) => onSnapshot(snapshot));
  socket.on('error', (e) => console.error('[ws] error', e));
  return socket;
}

async function hostFlow() {
  const hostId = `host-${Date.now()}`;
  console.log('== creating presentation ==');
  const deck = await http('POST', '/presentations', {
    title: 'StageVault Demo',
    hostId,
    scenes: [
      { title: 'Welcome', notes: 'Opening slide' },
      { title: 'Architecture' },
      { title: 'Live Q&A' },
    ],
  });
  console.log('presentation', deck.id);

  console.log('== publishing immutable version 1 ==');
  const v1 = await http('POST', `/presentations/${deck.id}/publish`, null, hostId);
  console.log('version', v1.version, 'scenes:', v1.scenes.map((s) => s.title).join(' / '));

  // Draft edit AFTER publish — the published version must remain untouched.
  await http('PUT', `/presentations/${deck.id}/scenes`, {
    scenes: [{ title: 'New draft only' }],
  }, hostId);

  console.log('== opening session (bound to immutable v1) ==');
  const session = await http('POST', '/sessions', {
    hostId,
    hostName: 'Host Demo',
    presentationId: deck.id,
    version: 1,
  });
  console.log('JOIN CODE:', session.code);

  const sock = connectSocket(
    session.id,
    hostId,
    -1,
    (e) => console.log(`[event] #${e.seq} ${e.type}`, e.payload),
    (s) => console.log(`[snapshot] status=${s.status} v=${s.version} participants=${s.participants.length} queue=${s.handQueue.length}`),
  );
  await sleep(300);

  let version = 0;
  const command = async (command, extra = {}) => {
    const res = await http(
      'POST',
      `/sessions/${session.code}/commands`,
      { requestId: rid(), expectedVersion: version, command, ...extra },
      hostId,
    );
    version = res.version;
    return res;
  };

  console.log('== lobby -> live ==');
  await command('start');
  console.log('== scene 0 -> 1 ==');
  await command('changeScene', { sceneIndex: 1 });
  console.log('== pause / resume ==');
  await command('pause');
  await command('resume');

  console.log('== idempotency demo: retry same requestId ==');
  const fixed = { requestId: 'demo-fixed-id', expectedVersion: version };
  await http('POST', `/sessions/${session.code}/commands`,
    { ...fixed, command: 'changeScene', sceneIndex: 2 }, hostId);
  version += 1;
  const retry = await http('POST', `/sessions/${session.code}/commands`,
    { ...fixed, command: 'changeScene', sceneIndex: 2 }, hostId);
  console.log('retry replayed at version', retry.version, '(no new event)');

  console.log('== version conflict demo (stale expectedVersion) ==');
  try {
    await http('POST', `/sessions/${session.code}/commands`,
      { requestId: rid(), expectedVersion: 0, command: 'pause' }, hostId);
  } catch (e) {
    console.log('conflict rejected:', e.body.error, '-', e.body.message);
  }
  await command('pause');

  console.log('== ending ==');
  await command('end');

  console.log('== restart after end must fail ==');
  try {
    await http('POST', `/sessions/${session.code}/commands`,
      { requestId: rid(), expectedVersion: version + 1, command: 'start' }, hostId);
  } catch (e) {
    console.log('rejected:', e.body.error);
  }

  await sleep(300);
  sock.close();
  console.log('done. JOIN CODE was', session.code);
}

async function participantFlow(code, userId, name) {
  console.log(`== joining ${code} as ${userId} (${name}) ==`);
  const joined = await http('POST', `/sessions/${code}/join`, { userId, name });
  console.log('joined, role:', joined.role);

  const sock = connectSocket(
    code,
    userId,
    -1,
    (e) => console.log(`[event] #${e.seq} ${e.type}`, e.payload),
    (s) => console.log(`[snapshot] status=${s.status} v=${s.version} scene=${s.currentSceneIndex} participants=${s.participants.length} queue=${s.handQueue.map((h) => h.name).join(',') || '-'}`),
  );

  // Read commands from stdin: "raise", "cancel", "snapshot".
  process.stdin.setEncoding('utf8');
  console.log('commands: raise | cancel | snapshot | quit');
  let version = joined.snapshot.version;
  for await (const line of process.stdin) {
    const action = line.trim();
    if (action === 'quit') break;
    if (action === 'snapshot') {
      const snap = await http('GET', `/sessions/${code}/snapshot`, null, userId);
      console.log(JSON.stringify(snap, null, 2));
      continue;
    }
    try {
      const res = await http('POST', `/sessions/${code}/commands`, {
        requestId: rid(),
        expectedVersion: version,
        command: action === 'raise' ? 'raiseHand' : 'cancelHand',
        name,
      }, userId);
      if (res.version) version = res.version;
      console.log('ok', res);
    } catch (e) {
      console.log('rejected:', e.body?.error ?? e.message);
    }
  }
  sock.close();
}

const [mode, code, userId, name] = process.argv.slice(2);
if (mode === 'host') {
  hostFlow().catch((e) => { console.error(e); process.exit(1); });
} else if (mode === 'participant' && code && userId) {
  participantFlow(code, userId, name || userId).catch((e) => { console.error(e); process.exit(1); });
} else {
  console.log('Usage: node examples/client.js host | participant <CODE> <USER_ID> [NAME]');
  process.exit(1);
}
