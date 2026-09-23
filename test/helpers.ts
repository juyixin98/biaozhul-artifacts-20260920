import { randomBytes } from "node:crypto";
import { createDeps, ServerDeps } from "../src/app";
import { makeCidV1 } from "../src/crypto/cid";
import { CODEC_JSON, CODEC_RAW } from "../src/crypto/cid";
import { FastifyInstance } from "fastify";
import { buildServer } from "../src/app";

export interface TestHarness {
  deps: ServerDeps;
  app: FastifyInstance;
}

export async function makeTestApp(): Promise<TestHarness> {
  const deps = createDeps(":memory:");
  const app = buildServer(deps, "silent");
  await app.ready();
  return { deps, app };
}

export async function closeHarness(h: TestHarness): Promise<void> {
  await h.app.close();
  h.deps.db.close();
}

export function cidFor(codec: number, data: Buffer): string {
  return makeCidV1(codec, data).canonical;
}

export const jsonCid = (data: Buffer) => cidFor(CODEC_JSON, data);
export const rawCid = (data: Buffer) => cidFor(CODEC_RAW, data);

/** Minimal valid 1x1 transparent PNG (67 bytes). */
export const PNG_1PX = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64"
);

/** 1x1 red PNG (67 bytes as well, different bytes -> different CID). */
export const PNG_RED = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64"
);

export function jpegBytes(): Buffer {
  // SOI + a couple of bytes; magic sniff only needs FF D8 FF.
  return Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46]);
}

export function makeMetadata(obj: Record<string, unknown>): Buffer {
  return Buffer.from(JSON.stringify(obj), "utf8");
}

export function seedGenesisAndConfirmations(
  app: FastifyInstance,
  heights = 3
): Promise<unknown>[] {
  // heights = number of blocks to append including genesis (0..heights-1)
  const ops: Promise<unknown>[] = [];
  for (let h = 0; h < heights; h++) {
    ops.push(
      app.inject({
        method: "POST",
        url: "/chain/blocks",
        payload: {
          action: "append",
          block_hash: "0x" + (0xa000 + h).toString(16).padStart(8, "0"),
          parent_hash: h === 0 ? null : "0x" + (0xa000 + h - 1).toString(16).padStart(8, "0"),
        },
      })
    );
  }
  return ops;
}

export async function appendBlock(
  app: FastifyInstance,
  hashHex: string,
  parentHex: string | null
): Promise<void> {
  const res = await app.inject({
    method: "POST",
    url: "/chain/blocks",
    payload: { action: "append", block_hash: hashHex, parent_hash: parentHex },
  });
  if (res.statusCode >= 400) throw new Error("appendBlock failed: " + res.body);
}

export function randomHash(tag = 1): string {
  return "0x" + (randomBytes(15).toString("hex") + tag.toString(16).padStart(2, "0")).slice(0, 40);
}

/** Put a block through the API (orchestrates base64 + CID). */
export async function putBlock(
  app: FastifyInstance,
  cid: string,
  data: Buffer
): Promise<{ statusCode: number; body: any }> {
  const res = await app.inject({
    method: "PUT",
    url: `/blocks/${encodeURIComponent(cid)}`,
    payload: { data_base64: data.toString("base64") },
  });
  return { statusCode: res.statusCode, body: res.json() };
}
