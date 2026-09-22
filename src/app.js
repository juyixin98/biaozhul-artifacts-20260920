const express = require('express');
const clockMiddleware = require('./middleware/clock');
const { authRequired } = require('./middleware/auth');
const errorHandler = require('./middleware/errorHandler');
const { questionRouter, paperRouter } = require('./routes/bank');
const examRoutes = require('./routes/exams');

const app = express();
app.use(express.json({ limit: '1mb' }));
app.use(clockMiddleware);

app.get('/health', (_req, res) => res.json({ status: 'ok', service: 'careops-assessment' }));

app.use('/api/questions', authRequired, questionRouter);
app.use('/api/papers', authRequired, paperRouter);
app.use('/api', authRequired, examRoutes);

app.use((_req, res) => res.status(404).json({ error: { code: 'NOT_FOUND', message: 'route not found' } }));
app.use(errorHandler);

module.exports = app;
