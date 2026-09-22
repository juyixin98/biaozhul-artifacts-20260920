module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.spec.ts', '**/*-spec.ts'],
  setupFiles: ['<rootDir>/test/jest-setup.ts'],
  testTimeout: 60000,
};
