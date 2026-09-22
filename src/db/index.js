'use strict';

const { Sequelize } = require('sequelize');
const config = require('../config');

// 测试使用独立库 careops_test；timezone 固定 +00:00，业务时区由组织字段决定
const dbName = process.env.NODE_ENV === 'test' ? `${config.db.name}_test` : config.db.name;

const sequelize = new Sequelize(dbName, config.db.user, config.db.password, {
  host: config.db.host,
  port: config.db.port,
  dialect: 'mysql',
  timezone: '+00:00',
  define: { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' },
  logging: false,
  pool: { max: 10, min: 0, acquire: 30000, idle: 10000 },
});

const models = {};

function def(name, modelFn) {
  models[name] = modelFn(sequelize, Sequelize.DataTypes);
  return models[name];
}

def('Organization', require('./models/organization'));
def('User', require('./models/user'));
def('Question', require('./models/question'));
def('Paper', require('./models/paper'));
def('PaperQuestion', require('./models/paperQuestion'));
def('ExamSession', require('./models/examSession'));
def('Submission', require('./models/submission'));
def('ScoreVersion', require('./models/scoreVersion'));
def('Certificate', require('./models/certificate'));
def('LearningActivity', require('./models/learningActivity'));

const {
  Organization, User, Question, Paper, PaperQuestion,
  ExamSession, Submission, ScoreVersion, Certificate, LearningActivity,
} = models;

// 关联
Organization.hasMany(User, { foreignKey: 'orgId' });
User.belongsTo(Organization, { as: 'organization', foreignKey: 'orgId' });

Organization.hasMany(Question, { foreignKey: 'orgId' });
Question.belongsTo(Organization, { as: 'organization', foreignKey: 'orgId' });

Organization.hasMany(Paper, { foreignKey: 'orgId' });
Paper.belongsTo(Organization, { as: 'organization', foreignKey: 'orgId' });

Paper.hasMany(PaperQuestion, { foreignKey: 'paperId', as: 'questions' });
PaperQuestion.belongsTo(Paper, { foreignKey: 'paperId' });
PaperQuestion.belongsTo(Question, { as: 'question', foreignKey: 'questionId' });

Paper.hasMany(ExamSession, { foreignKey: 'paperId' });
ExamSession.belongsTo(Paper, { as: 'paper', foreignKey: 'paperId' });
User.hasMany(ExamSession, { foreignKey: 'userId' });
ExamSession.belongsTo(User, { as: 'user', foreignKey: 'userId' });

ExamSession.hasMany(Submission, { foreignKey: 'sessionId' });
Submission.belongsTo(ExamSession, { as: 'session', foreignKey: 'sessionId' });

ExamSession.hasMany(ScoreVersion, { foreignKey: 'sessionId', as: 'scoreVersions' });
ScoreVersion.belongsTo(ExamSession, { as: 'session', foreignKey: 'sessionId' });

ExamSession.hasOne(Certificate, { foreignKey: 'sessionId' });
Certificate.belongsTo(ExamSession, { as: 'session', foreignKey: 'sessionId' });
Certificate.belongsTo(User, { as: 'user', foreignKey: 'userId' });

User.hasMany(LearningActivity, { foreignKey: 'userId' });
LearningActivity.belongsTo(User, { as: 'user', foreignKey: 'userId' });

module.exports = { sequelize, Sequelize, ...models };
