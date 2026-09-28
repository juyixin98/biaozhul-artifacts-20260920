/**
 * Synthetic local sources. Every value comes from data/fixtures.json; every
 * source is a FixtureSource honoring deadline + abort. No network anywhere.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { FixtureSource } from './fixtures/fixtureSource.js';
import { FaultPlan } from './fixtures/faults.js';
import { SourceRegistry } from './registry.js';
import { computationFailed } from '../kernel/errors.js';

const here = dirname(fileURLToPath(import.meta.url));
// src/sources/  ->  <root>/data ; dist/sources/ -> <root>/data as well
const fixturePath = join(here, '../../data/fixtures.json');
interface UserProfile {
  userId: string;
  displayName: string;
  loyaltyTier: string;
  region: string;
}
interface Contact {
  email: string;
  phone: string;
}
interface Product {
  sku: string;
  listPrice: number;
  currency: string;
}
interface Availability {
  available: boolean;
  warehouseCount: number;
}
interface Recommendation {
  recommendedSku: string;
  confidence: number;
}
interface Promotion {
  promotionCode: string | null;
  discountPct: number | null;
}

interface FixtureData {
  users: Record<string, UserProfile>;
  contacts: Record<string, Contact>;
  catalog: Record<string, Product>;
  loyaltyDiscount: Record<string, number>;
  availability: Record<string, Record<string, Availability>>;
  recommendations: Record<string, Record<string, Recommendation>>;
  promotions: Record<string, Promotion>;
}

const data = JSON.parse(readFileSync(fixturePath, 'utf8')) as FixtureData;

export interface BuildSourcesOptions {
  faultPlan?: FaultPlan;
  /** Contacts source is the legacy system that cannot pin snapshots. */
  contactSnapshotCapable?: boolean;
  /** Data version the pricing source reports under a pinned snapshot. */
  pricingDataVersion?: string;
  /** Per-source simulated latency in ms. */
  baseLatencyMs?: number;
  /** Source names to omit (tests register an instrumented replacement). */
  skip?: readonly string[];
}

const DEFAULT_VERSION = 'catalog-epoch-2026-09-01';

export function buildSources(options: BuildSourcesOptions = {}): SourceRegistry {
  const faultPlan = options.faultPlan ?? new FaultPlan();
  const latency = options.baseLatencyMs ?? 4;
  const skip = new Set(options.skip ?? []);
  const registry = new SourceRegistry();
  const registerIf = (name: string, source: FixtureSource): void => {
    if (!skip.has(name)) registry.register(source);
  };

  const users = new FixtureSource(
    'users',
    { snapshot: true, version: DEFAULT_VERSION },
    { faultPlan, baseLatencyMs: latency },
  ).method('getProfile', (req: { userId: string }) => {
    const profile = data.users[req.userId];
    if (!profile) {
      throw computationFailed('SOURCE_NOT_FOUND', `user not found: ${req.userId}`, {
        retryable: false,
        context: { userId: req.userId },
      });
    }
    return profile;
  });
  registerIf('users', users);

  const contactsCapable = options.contactSnapshotCapable ?? false;
  const contacts = new FixtureSource(
    'contacts',
    { snapshot: contactsCapable, version: contactsCapable ? DEFAULT_VERSION : 'contacts-legacy-unversioned' },
    { faultPlan, baseLatencyMs: latency },
  )
    .method('getContact', (req: { userId: string }) => {
      const contact = data.contacts[req.userId];
      if (!contact) {
        throw computationFailed('SOURCE_NOT_FOUND', `contact not found: ${req.userId}`, {
          context: { userId: req.userId },
        });
      }
      return contact;
    })
    // Legacy source honestly reports that it cannot pin snapshots.
    .reportConsistency(() => ({
      snapshotHonored: contactsCapable,
      dataVersion: contactsCapable ? DEFAULT_VERSION : 'unstamped-live-read',
    }));
  registerIf('contacts', contacts);

  const catalog = new FixtureSource(
    'catalog',
    { snapshot: true, version: DEFAULT_VERSION },
    { faultPlan, baseLatencyMs: latency },
  ).method('getProduct', (req: { sku: string }) => {
    const product = data.catalog[req.sku];
    if (!product) {
      throw computationFailed('SOURCE_NOT_FOUND', `product not found: ${req.sku}`, {
        context: { sku: req.sku },
      });
    }
    return product;
  });
  registerIf('catalog', catalog);

  const inventory = new FixtureSource(
    'inventory',
    { snapshot: true, version: DEFAULT_VERSION },
    { faultPlan, baseLatencyMs: latency },
  ).method('getAvailability', (req: { region: string; sku: string }) => {
    const entry = data.availability[req.region]?.[req.sku];
    if (!entry) {
      throw computationFailed('SOURCE_NOT_FOUND', `availability unknown for ${req.region}/${req.sku}`, {
        context: { region: req.region, sku: req.sku },
      });
    }
    return entry;
  });
  registerIf('inventory', inventory);

  const pricingVersion = options.pricingDataVersion ?? DEFAULT_VERSION;
  const pricing = new FixtureSource(
    'pricing',
    { snapshot: true, version: pricingVersion },
    { faultPlan, baseLatencyMs: latency },
  ).method('getQuote', (req: { sku: string; loyaltyTier: string }) => {
    const product = data.catalog[req.sku];
    if (!product) {
      throw computationFailed('SOURCE_NOT_FOUND', `cannot price unknown sku: ${req.sku}`, {
        context: { sku: req.sku },
      });
    }
    const discountPct = data.loyaltyDiscount[req.loyaltyTier] ?? 0;
    return {
      sku: req.sku,
      currency: product.currency,
      listPrice: product.listPrice,
      discountPct,
      finalPrice: Math.round(product.listPrice * (1 - discountPct) * 100) / 100,
    };
  }).reportConsistency(() => ({
    snapshotHonored: true,
    dataVersion: pricingVersion,
  }));
  registerIf('pricing', pricing);

  const recommendations = new FixtureSource(
    'recommendations',
    { snapshot: true, version: DEFAULT_VERSION },
    { faultPlan, baseLatencyMs: latency },
  ).method('getRecommendation', (req: { userId: string; sku: string }) => {
    const hit = data.recommendations[req.userId]?.[req.sku];
    if (!hit) {
      throw computationFailed('SOURCE_NOT_FOUND', `no recommendation for ${req.userId}/${req.sku}`, {
        context: { userId: req.userId, sku: req.sku },
      });
    }
    return hit;
  });
  registerIf('recommendations', recommendations);

  const promotions = new FixtureSource(
    'promotions',
    { snapshot: true, version: DEFAULT_VERSION },
    { faultPlan, baseLatencyMs: latency },
  ).method('getPromotion', (req: { sku: string }) => {
    const promo = data.promotions[req.sku];
    if (!promo) {
      throw computationFailed('SOURCE_NOT_FOUND', `no promotion for ${req.sku}`, {
        context: { sku: req.sku },
      });
    }
    // promotionCode is null for sku-2 → optional field resolves as missing.
    return promo;
  });
  registerIf('promotions', promotions);

  return registry;
}
