'use strict';

const express = require('express');
const routes = require('./routes');
const errorHandler = require('./middleware/errorHandler');
const clock = require('./config/clock');

function createApp() {
  const app = express();
  app.use(express.json({ limit: '1mb' }));

  app.get('/health', (req, res) => res.json({ ok: true, time: clock.now().toISOString() }));
  app.use('/api', routes);

  app.use((req, res) => res.status(404).json({ error: { code: 'NOT_FOUND', message: '接口不存在' } }));
  app.use(errorHandler);
  return app;
}

module.exports = { createApp };
