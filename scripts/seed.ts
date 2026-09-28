/**
 * 显式播种脚本：删除并重建样例对象。
 * 用法：npm run seed [-- --db data/objects.db]
 */
import { parseArgs } from 'node:util';
import { SAMPLE_OBJECTS } from '../fixtures/samples.js';
import { strongEtag } from '../src/store/etag.js';
import { SqliteObjectStore } from '../src/store/sqliteStore.js';

const { values } = parseArgs({
  options: { db: { type: 'string', default: 'data/objects.db' } },
});

const store = new SqliteObjectStore(values.db ?? 'data/objects.db');
for (const sample of SAMPLE_OBJECTS) {
  const etag = strongEtag(sample.content);
  store.putObject({
    id: sample.id,
    content: sample.content,
    contentType: sample.contentType,
    lastModifiedMs: sample.lastModifiedMs,
    etag,
  });
  console.log(
    `[seed] id=${sample.id} size=${sample.content.length} etag=${etag} -- ${sample.description}`,
  );
}
store.close();
