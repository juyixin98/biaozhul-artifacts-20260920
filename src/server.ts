import { buildApp } from './app.js';

const { app, services, close } = buildApp();

process.on('SIGINT', () => {
  void close().then(() => process.exit(0));
});
process.on('SIGTERM', () => {
  void close().then(() => process.exit(0));
});

app.listen({ host: services.config.host, port: services.config.port }).then(() => {
  app.log.info(
    {
      host: services.config.host,
      port: services.config.port,
      db: services.config.dbPath,
      confirmations: services.config.confirmations,
      limits: {
        block: services.config.maxBlockSize,
        metadata: services.config.maxMetadataBytes,
        media: services.config.maxMediaBytes,
      },
    },
    'NFT metadata consistency service listening',
  );
});
