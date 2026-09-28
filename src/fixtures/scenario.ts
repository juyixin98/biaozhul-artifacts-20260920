import { DatabaseSync } from 'node:sqlite';
import { parseContract, type RawContract, type RawNode } from '../contract/parser.js';
import { FixtureSource } from '../sources/fixtureSource.js';
import { SqliteSource } from '../sources/sqliteSource.js';
import type { CompositeContract, DataSource } from '../types.js';

/**
 * ============================================================================
 * Local synthetic scenario: "orderDetails" composite.
 *
 * Dependency graph (a textbook diamond + an independent branch):
 *
 *                        customers (A)
 *                       /            \
 *               inventory (B)    promotions (C, optional)
 *                      \            /
 *                   recommendations (D = B ⋈ C)        pricing (independent)
 *
 * Diamond: A→B→D and A→C→D. The promotions edge into D is optional (C is an
 * optional node), so when C times out D still runs with promoDiscount=null.
 * The kernel must invoke D exactly once despite two (three, counting A→D)
 * paths reaching it.
 *
 * Every participant is local: in-memory fixture tables or a seeded local
 * SQLite database. No production accounts or network calls.
 * ============================================================================
 */

export const ORDER_DETAILS_CONTRACT: RawContract = {
  name: 'orderDetails',
  version: 1,
  nodes: [
    { id: 'customers', source: 'customers-db', params: { customerId: { request: 'customerId' } } },
    {
      id: 'inventory',
      source: 'inventory-fixture',
      params: {
        sku: { request: 'sku' },
        // Edge A→B: inventory is scoped by the customer's region.
        region: { from: 'customers', property: 'region' },
      },
    },
    { id: 'pricing', source: 'pricing-db', params: { sku: { request: 'sku' } } },
    {
      id: 'promotions',
      source: 'promotions-fixture',
      necessity: 'optional',
      timeoutMs: 80,
      params: { customerId: { from: 'customers', property: 'customer_id' } },
    },
    {
      id: 'recommendations',
      source: 'recommendations-fixture',
      params: {
        customerId: { from: 'customers', property: 'customer_id' },
        sku: { request: 'sku' },
        // Edge B→D: recommendation depends on current stock.
        stock: { from: 'inventory', property: 'stock' },
        // Edge C→D (optional): when promotions times out, null is passed and D runs.
        promoDiscount: { from: 'promotions', property: 'discount' },
      },
    },
  ],
  fields: [
    { path: 'customer.name', required: true, ref: { from: 'customers', property: 'name' } },
    { path: 'customer.tier', required: false, ref: { from: 'customers', property: 'tier' } },
    { path: 'product.name', required: true, ref: { from: 'inventory', property: 'name' } },
    { path: 'product.stock', required: true, ref: { from: 'inventory', property: 'stock' } },
    { path: 'price', required: true, ref: { from: 'pricing', property: 'price' } },
    { path: 'fee', required: true, ref: { constant: 5 } },
    {
      path: 'discount',
      required: false,
      ref: { from: 'promotions', property: 'discount', fallback: 0 },
    },
    {
      path: 'recommendation',
      required: false,
      ref: { from: 'recommendations', property: 'reason', fallback: 'generic-bundle' },
    },
    {
      path: 'label',
      required: true,
      ref: { derive: { op: 'concat', fields: ['customer.name', 'product.name'], separator: ' / ' } },
    },
    {
      path: 'total',
      required: true,
      ref: { derive: { op: 'add', fields: ['price', 'fee'] } },
    },
  ],
};

export interface ScenarioOverrides {
  /** Customers participant latency (default 8ms). */
  customersLatencyMs?: number;
  /** Promotions participant latency (default fast). */
  promotionsLatencyMs?: number;
  promotionsHang?: boolean;
  /** Data version served by the pricing source (default v1). */
  pricingVersion?: string;
  pricingSupportsSnapshot?: boolean;
  pricingLatencyMs?: number;
  /** Make the pricing participant deterministically fail for a sku. */
  pricingFailForSku?: string;
  /** Inventory snapshot capability / contract requirement. */
  inventorySupportsSnapshot?: boolean;
  inventorySnapshotExact?: boolean;
  inventoryVersion?: string;
  inventoryHealthy?: boolean;
  inventoryFailForSku?: string;
  inventoryLatencyMs?: number;
  /** Recommendations source snapshot support (default: unsupported). */
  recommendationsSupportsSnapshot?: boolean;
  recommendationsLatencyMs?: number;
  clock?: () => number;
}

export interface Scenario {
  contract: CompositeContract;
  rawContract: RawContract;
  sources: Map<string, DataSource>;
  /** Typed handles for call-count / cancellation assertions. */
  handles: {
    customers: SqliteSource;
    inventory: FixtureSource;
    pricing: SqliteSource;
    promotions: FixtureSource;
    recommendations: FixtureSource;
  };
  db: DatabaseSync;
}

export function buildScenario(overrides: ScenarioOverrides = {}): Scenario {
  const db = seedDatabase();

  const rawContract: RawContract = JSON.parse(JSON.stringify(ORDER_DETAILS_CONTRACT)) as RawContract;
  if (overrides.inventorySnapshotExact) {
    const nodes = rawContract.nodes as RawNode[];
    const inventoryNode = nodes.find((n) => n.id === 'inventory');
    if (inventoryNode) inventoryNode.snapshot = 'exact';
  }

  const contract = parseContract(rawContract);

  const customers = new SqliteSource('customers-db', {
    db,
    sql: 'SELECT customer_id, name, tier, region FROM customers WHERE customer_id = $customerId',
    behavior: {
      latencyMs: overrides.customersLatencyMs ?? 8,
      dataVersion: 'v1',
      supportsSnapshot: true,
    },
    clock: overrides.clock,
  });

  const inventory = new FixtureSource('inventory-fixture', {
    clock: overrides.clock,
    behavior: {
      latencyMs: overrides.inventoryLatencyMs ?? 12,
      dataVersion: overrides.inventoryVersion ?? 'v1',
      supportsSnapshot: overrides.inventorySupportsSnapshot ?? true,
      healthy: overrides.inventoryHealthy ?? true,
      ...(overrides.inventoryFailForSku
        ? { failWhen: [{ param: 'sku', equals: overrides.inventoryFailForSku, code: 'INVENTORY_BACKEND_ERROR' }] }
        : {}),
    },
    table: {
      lookupParam: 'sku',
      rows: [
        { sku: 'SKU-1', name: 'Widget', stock: 7 },
        { sku: 'SKU-2', name: 'Gadget', stock: 0 },
        { sku: 'SKU-BROKEN', name: 'Mystery', stock: 3 },
        { sku: 'SKU-DRIFT', name: 'Drifter', stock: 21 },
      ],
    },
  });

  const pricing = new SqliteSource('pricing-db', {
    db,
    sql: 'SELECT sku, price, currency FROM pricing WHERE sku = $sku',
    behavior: {
      latencyMs: overrides.pricingLatencyMs ?? 6,
      dataVersion: overrides.pricingVersion ?? 'v1',
      supportsSnapshot: overrides.pricingSupportsSnapshot ?? true,
      ...(overrides.pricingFailForSku
        ? { failWhen: [{ param: 'sku', equals: overrides.pricingFailForSku, code: 'PRICING_BACKEND_ERROR' }] }
        : {}),
    },
    clock: overrides.clock,
  });

  const promotions = new FixtureSource('promotions-fixture', {
    clock: overrides.clock,
    behavior: {
      latencyMs: overrides.promotionsLatencyMs ?? 5,
      hangForever: overrides.promotionsHang ?? false,
      dataVersion: 'v1',
      supportsSnapshot: true,
    },
    table: {
      lookupParam: 'customerId',
      rows: [{ customerId: 'C1', discount: 15 }],
    },
  });

  const recommendations = new FixtureSource('recommendations-fixture', {
    clock: overrides.clock,
    behavior: {
      latencyMs: overrides.recommendationsLatencyMs ?? 4,
      dataVersion: 'v1',
      supportsSnapshot: overrides.recommendationsSupportsSnapshot ?? false,
    },
    table: {
      lookupParam: 'customerId',
      rows: [
        { customerId: 'C1', reason: 'bundle Widget + Gadget at 10% off' },
        { customerId: 'C2', reason: 'restock alert' },
      ],
    },
  });

  const sources = new Map<string, DataSource>([
    ['customers-db', customers],
    ['inventory-fixture', inventory],
    ['pricing-db', pricing],
    ['promotions-fixture', promotions],
    ['recommendations-fixture', recommendations],
  ]);

  return {
    contract,
    rawContract,
    sources,
    handles: { customers, inventory, pricing, promotions, recommendations },
    db,
  };
}

/** Canonical happy-path input; independent expected values live in tests. */
export const HAPPY_INPUT = { customerId: 'C1', sku: 'SKU-1' };

/** Snapshot token encoding logical version v1 (format: snap-<version>-<id>). */
export function snapshotToken(version = 'v1'): string {
  return `snap-${version}-20260928T000000Z`;
}

function seedDatabase(): DatabaseSync {
  const db = new DatabaseSync(':memory:');
  db.exec(`
    CREATE TABLE customers (
      customer_id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      tier TEXT NOT NULL,
      region TEXT NOT NULL
    );
    CREATE TABLE pricing (
      sku TEXT PRIMARY KEY,
      price INTEGER NOT NULL,
      currency TEXT NOT NULL
    );
  `);
  const insertCustomer = db.prepare('INSERT INTO customers VALUES (?, ?, ?, ?)');
  for (const row of [
    ['C1', 'Alice', 'gold', 'cn-north'],
    ['C2', 'Bob', 'silver', 'cn-east'],
    ['C3', 'Carol', 'bronze', 'cn-south'],
  ]) {
    insertCustomer.run(...row);
  }
  const insertPricing = db.prepare('INSERT INTO pricing VALUES (?, ?, ?)');
  for (const row of [
    ['SKU-1', 100, 'CNY'],
    ['SKU-2', 25, 'CNY'],
    ['SKU-BROKEN', 999, 'CNY'],
    ['SKU-DRIFT', 200, 'CNY'],
  ]) {
    insertPricing.run(...row);
  }
  return db;
}
