#!/usr/bin/env node
// Creates (or reuses) the careops_test database and applies migrations.
process.env.DB_NAME = process.env.DB_NAME || 'careops_test';
require('../src/db/migrate.js');
