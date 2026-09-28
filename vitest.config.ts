import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    testTimeout: 20000,
    hookTimeout: 20000,
    reporters: ['default'],
    coverage: { enabled: false },
    pool: 'threads',
    poolOptions: { threads: { singleThread: true } }
  }
});
