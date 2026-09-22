import { execSync } from 'child_process';

export default async function globalTeardown() {
  const container = process.env.__TEST_PG_CONTAINER;
  if (container && !process.env.KEEP_TEST_DB) {
    try {
      execSync(`docker rm -f ${container}`, { stdio: 'ignore' });
    } catch {
      // best effort
    }
  }
}
