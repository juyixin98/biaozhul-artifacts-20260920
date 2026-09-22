/**
 * StageVault example client (Node 18+).
 *
 * Demonstrates the full flow:
 *   1. create a presentation + version, publish it
 *   2. create a session (host) and join as a participant
 *   3. subscribe over WebSocket and keep state in sync
 *   4. host sends idempotent commands (requestId + expectedVersion)
 *   5. simulate a disconnect and catch up from the last applied seq
 *
 * Run:  node client/example-client.js
 * Env:  BASE_URL (default http://localhost:3000)
 */
const WebSocket = require('ws');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const WS_URL = BASE_URL.replace(/^http/, 'ws') + '/ws';

async function api(method, path, { token, body } = {}) {
  const res = await fetch(BASE_URL + path, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { 'x-session-token': token } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(`${method} ${path} -> ${res.status}`);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

/** Minimal sync client: applies events in order, never rolls back. */
class SessionClient {
  constructor(sessionId, token) {
    this.sessionId = sessionId;
    this.token = token;
    this.lastSeq = 0; // last applied event seq
    this.state = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(WS_URL);
      this.ws.on('open', () => {
        // Ask for everything after the last seq we applied.
        this.ws.send(
          JSON.stringify({
            type: 'subscribe',
            sessionId: this.sessionId,
            token: this.token,
            lastSeq: this.lastSeq,
          }),
        );
      });
      this.ws.on('message', (raw) => {
        const msg = JSON.parse(raw.toString());
        if (msg.type === 'snapshot') {
          // Missing history: replace local state with the server snapshot.
          this.state = msg.state;
          this.lastSeq = msg.seq;
        } else if (msg.type === 'event') {
          // Duplicate / out-of-order deliveries must not roll state back.
          if (msg.seq <= this.lastSeq) return;
          if (this.state) {
            if (msg.payload.status) this.state.status = msg.payload.status;
            if (msg.payload.sceneIndex !== undefined) {
              this.state.currentSceneIndex = msg.payload.sceneIndex;
            }
            this.state.eventSeq = msg.seq;
          }
          this.lastSeq = msg.seq;
          console.log(`  [event #${msg.seq}] ${msg.eventType}`, msg.payload);
        } else if (msg.type === 'subscribed') {
          resolve();
        } else if (msg.type === 'error') {
          reject(new Error(`${msg.code}: ${msg.message}`));
        }
      });
      this.ws.on('close', () => {
        // Reconnect and catch up from our last applied seq.
        if (!this.closed) {
          setTimeout(() => this.connect().catch(() => {}), 500);
        }
      });
      this.ws.on('error', () => {});
    });
  }

  close() {
    this.closed = true;
    this.ws?.close();
  }
}

async function main() {
  // 1. presentation + immutable published version
  const presentation = await api('POST', '/presentations', {
    body: { title: 'Quarterly Demo' },
  });
  const version = await api('POST', `/presentations/${presentation.id}/versions`, {
    body: {
      scenes: [
        { title: 'Intro', content: 'Welcome' },
        { title: 'Agenda', content: 'Three topics' },
        { title: 'Wrap-up', content: 'Q&A' },
      ],
    },
  });
  await api('POST', `/presentation-versions/${version.id}/publish`);

  // 2. session + participant
  const session = await api('POST', '/sessions', {
    body: { presentationVersionId: version.id, hostName: 'Host' },
  });
  console.log(`session created, join code: ${session.joinCode}`);
  const guest = await api('POST', '/sessions/join', {
    body: { joinCode: session.joinCode, name: 'Guest' },
  });

  // 3. participant subscribes via WebSocket
  const client = new SessionClient(session.sessionId, guest.token);
  await client.connect();
  console.log('participant subscribed, state:', client.state?.status);

  // 4. host drives the presentation with idempotent commands
  let version_ = 0;
  const command = async (type, extra = {}) => {
    const result = await api('POST', `/sessions/${session.sessionId}/commands`, {
      token: session.hostToken,
      body: { requestId: `demo-${type}-${version_}`, expectedVersion: version_, type, ...extra },
    });
    version_ = result.version;
    return result;
  };
  await command('start');
  await command('goto_scene', { sceneIndex: 1 });

  // idempotent retry: same requestId returns the original result
  const retry = await api('POST', `/sessions/${session.sessionId}/commands`, {
    token: session.hostToken,
    body: { requestId: 'demo-goto_scene-1', expectedVersion: 1, type: 'goto_scene', sceneIndex: 1 },
  });
  console.log('idempotent retry returned replayed =', retry.replayed);

  // 5. participant raises a hand, host resolves it
  await api('POST', `/sessions/${session.sessionId}/hand-raises`, { token: guest.token });
  await api('POST', `/sessions/${session.sessionId}/hand-raises/${guest.participantId}/resolve`, {
    token: session.hostToken,
  });

  // 6. simulate reconnect: the client catches up from its last seq
  client.ws.terminate();
  await new Promise((r) => setTimeout(r, 1200));
  console.log('reconnected, last applied seq:', client.lastSeq);

  await command('end');
  await new Promise((r) => setTimeout(r, 300));
  console.log('final state:', client.state?.status, 'seq', client.lastSeq);
  client.close();
  process.exit(0);
}

main().catch((e) => {
  console.error(e.body || e);
  process.exit(1);
});
