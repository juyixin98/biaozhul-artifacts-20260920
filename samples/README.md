This directory contains small deterministic raw/dd sample disk images for
experiments and tests. They are flat byte patterns produced by
`cmd/genseed`; they contain no filesystem and no real data.

Regenerate with:

    go run ./cmd/genseed ./samples

Bundled files (sha-256 over the whole file):

- sample_1mb.dd   1,048,576 bytes
  sha256: 86d9a56b58e63c591af776487b0927b79bc115ba58264c6888ab503d5c23e081
- sample_256k.raw   262,144 bytes
  sha256: 6236c1e655f45673e9052261ed1f2f06db1b7d4e63c36bd84c3ee24dfbd23998

These samples are NOT evidence and the hashes are just recorded for quick
manual verification.
