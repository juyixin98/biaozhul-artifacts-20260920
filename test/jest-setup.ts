// Runs before any test module is imported, so AppModule picks these up.
process.env.DATABASE_URL =
  process.env.TEST_DATABASE_URL ||
  'postgres://postgres:postgres@localhost:5432/stagevault_test';
process.env.SESSION_SWEEP_DISABLED = 'true'; // tests call sweepOnce() directly
process.env.SESSION_TIMEOUT_MS = process.env.SESSION_TIMEOUT_MS || '1800000';
