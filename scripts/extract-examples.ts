import { readFileSync, writeFileSync } from 'node:fs';
const src = readFileSync('tests/fixtures/contracts.ts', 'utf8');
const grab = (marker: string, endMarker: string): string => {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`${marker} not found`);
  const bodyStart = start + marker.length;
  const end = src.indexOf(endMarker, bodyStart);
  if (end < 0) throw new Error(`${endMarker} not found`);
  return src.slice(bodyStart, end);
};
writeFileSync('examples/pets-v1.yaml', grab('const OLD = `', '`'));
writeFileSync('examples/pets-v2.yaml', grab('const NEW = `', '`'));
