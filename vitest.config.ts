import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: ['src/**/*.ts'],
      exclude: [
        'src/main.ts',
        // 纯类型声明文件，编译后无运行时代码
        'src/graphql/ast.ts',
      ],
    },
    testTimeout: 15000,
  },
});
