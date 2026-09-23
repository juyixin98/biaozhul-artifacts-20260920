import Database from "better-sqlite3";
import { Cid } from "../crypto/cid";

export interface BlockRecord {
  cid: string;
  version: 0 | 1;
  codec: number;
  hashCode: number;
  size: number;
  data: Buffer;
}

export class BlockStore {
  constructor(private readonly db: Database.Database) {}

  /** Direct handle for tests simulating storage-level tampering. */
  get rawDb(): Database.Database {
    return this.db;
  }

  /**
   * Persist a content block whose CID was derived from the bytes themselves.
   * Bytes have already been re-hashed and compared before this is called, so
   * stored rows can never disagree with their CID.
   */
  put(cid: Cid, data: Buffer): { cid: string; existed: boolean } {
    const canonical = cid.canonical;
    const existing = this.getRecord(canonical);
    if (existing) {
      // Same CID == same bytes (sha2-256 collision assumed absent); idempotent.
      return { cid: canonical, existed: true };
    }
    this.db
      .prepare(
        `INSERT INTO blocks (cid, version, codec, hash_code, size, data)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      .run(
        canonical,
        cid.version,
        cid.codec,
        cid.multihash.code,
        data.length,
        data
      );
    return { cid: canonical, existed: false };
  }

  /** Test seam: insert a row with bytes NOT matching its claimed CID.
   *  Used only by tests to exercise tamper handling at the storage layer. */
  putUntrustedForTest(canonicalCid: string, codec: number, data: Buffer): void {
    this.db
      .prepare(
        `INSERT INTO blocks (cid, version, codec, hash_code, size, data)
         VALUES (?, 1, ?, 18, ?, ?)`
      )
      .run(canonicalCid, codec, data.length, data);
  }

  getBytes(canonicalCid: string): Buffer | null {
    const row = this.db
      .prepare(`SELECT data FROM blocks WHERE cid = ?`)
      .get(canonicalCid) as { data: Buffer } | undefined;
    return row ? Buffer.from(row.data) : null;
  }

  getRecord(canonicalCid: string): BlockRecord | null {
    const row = this.db
      .prepare(
        `SELECT cid, version, codec, hash_code AS hashCode, size, data
         FROM blocks WHERE cid = ?`
      )
      .get(canonicalCid) as BlockRecord | undefined;
    return row ?? null;
  }

  list(): Array<Omit<BlockRecord, "data">> {
    return this.db
      .prepare(
        `SELECT cid, version, codec, hash_code AS hashCode, size
         FROM blocks ORDER BY cid`
      )
      .all() as Array<Omit<BlockRecord, "data">>;
  }

  count(): number {
    return (this.db.prepare(`SELECT count(*) AS n FROM blocks`).get() as { n: number }).n;
  }
}
