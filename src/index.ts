import { buildServer, createDeps } from "./app";
import { loadConfig } from "./config";

const cfg = loadConfig();
const deps = createDeps(cfg.dbPath);
const server = buildServer(deps, cfg.logLevel);

const shutdown = async () => {
  server.log.info("shutting down");
  await server.close();
  deps.db.close();
  process.exit(0);
};
process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());

server
  .listen({ host: cfg.host, port: cfg.port })
  .then(() => {
    server.log.info(`NFT metadata service listening on http://${cfg.host}:${cfg.port}`);
    server.log.info(`sqlite database: ${cfg.dbPath}`);
  })
  .catch((err) => {
    server.log.error(err);
    process.exit(1);
  });
