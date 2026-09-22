const crypto = require('crypto');

const MS_PER_DAY = 86400000;
const MS_PER_MINUTE = 60000;

// Deterministic 32-bit PRNG (mulberry32 over a splitmix32-seeded state).
// Same (seed, input sequence) always yields the same shuffle — this is what
// makes paper composition reproducible from paper.selectionSeed.
function splitmix32(str) {
  let h = 1779033703 ^ str.length;
  for (let i = 0; i < str.length; i++) {
    h = Math.imul(h ^ str.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return function () {
    h = Math.imul(h ^ (h >>> 16), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    h ^= h >>> 16;
    return h >>> 0;
  };
}

function mulberry32(seedStr) {
  let a = splitmix32(String(seedStr))();
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Fisher–Yates with the injected RNG; does not mutate the input array.
function shuffle(items, rng) {
  const arr = items.slice();
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

// In-place array comparison helper for answer sets (order-insensitive).
function sameAnswerSet(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const sa = a.slice().map(String).sort();
  const sb = b.slice().map(String).sort();
  return sa.every((v, i) => v === sb[i]);
}

// Canonical JSON so identical answer objects always hash identically.
function canonicalJson(value) {
  return JSON.stringify(sortValue(value));
}

function sortValue(value) {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value && typeof value === 'object') {
    return Object.keys(value)
      .sort()
      .reduce((acc, k) => {
        acc[k] = sortValue(value[k]);
        return acc;
      }, {});
  }
  return value;
}

function sha256(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

// Organization-timezone calendar day key, YYYY-MM-DD.
// Node's full-ICU Intl resolves IANA zones (e.g. Asia/Shanghai).
function dayKeyInTimezone(date, timezone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone || 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

function addMinutes(date, minutes) {
  return new Date(date.getTime() + minutes * MS_PER_MINUTE);
}

// Whole 30-day inactivity blocks elapsed since `since`. Boundary rule:
// at exactly 30*24h the first block counts (inclusive boundary).
function wholePeriodsElapsed(now, since, periodDays) {
  const delta = now.getTime() - since.getTime();
  return Math.max(0, Math.floor(delta / (periodDays * MS_PER_DAY)));
}

// Add calendar months (certificate validity = 12 months), end-of-month safe.
function addMonths(date, months) {
  const d = new Date(date.getTime());
  const day = d.getUTCDate();
  d.setUTCMonth(d.getUTCMonth() + months);
  if (d.getUTCDate() < day) d.setUTCDate(0);
  return d;
}

module.exports = {
  mulberry32,
  shuffle,
  sameAnswerSet,
  canonicalJson,
  sha256,
  dayKeyInTimezone,
  addMinutes,
  addMonths,
  wholePeriodsElapsed,
};
