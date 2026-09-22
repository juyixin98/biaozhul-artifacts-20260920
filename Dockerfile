# syntax=docker/dockerfile:1

# ---- build ----
FROM node:18-bookworm-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY tsconfig.json nest-cli.json ./
COPY src ./src
RUN npm run build

# ---- runtime ----
FROM node:18-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY package*.json ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --from=build /app/dist ./dist
EXPOSE 3000
# Run migrations, then start the API. Migrations also gate the app on
# Postgres being reachable.
CMD ["sh", "-c", "node dist/migrate.js && node dist/main.js"]
