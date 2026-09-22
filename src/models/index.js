const sequelize = require('../db/connection');

const Organization = require('./organization')(sequelize);
const User = require('./user')(sequelize);
const Question = require('./question')(sequelize);
const Paper = require('./paper')(sequelize);
const PaperQuestion = require('./paperQuestion')(sequelize);
const Exam = require('./exam')(sequelize);
const DailyAttemptCount = require('./dailyAttemptCount')(sequelize);
const Submission = require('./submission')(sequelize);
const ScoreVersion = require('./scoreVersion')(sequelize);
const WrongQuestion = require('./wrongQuestion')(sequelize);
const Certificate = require('./certificate')(sequelize);

Organization.hasMany(User, { foreignKey: 'organizationId' });
User.belongsTo(Organization, { foreignKey: 'organizationId' });

Organization.hasMany(Paper, { foreignKey: 'organizationId' });
Paper.belongsTo(Organization, { foreignKey: 'organizationId' });

Organization.hasMany(Question, { foreignKey: 'organizationId' });
Question.belongsTo(Organization, { foreignKey: 'organizationId' });

Paper.hasMany(PaperQuestion, { foreignKey: 'paperId', as: 'questions' });
PaperQuestion.belongsTo(Paper, { foreignKey: 'paperId' });

Paper.hasMany(Exam, { foreignKey: 'paperId' });
Exam.belongsTo(Paper, { foreignKey: 'paperId' });
User.hasMany(Exam, { foreignKey: 'userId' });
Exam.belongsTo(User, { foreignKey: 'userId' });

Exam.hasMany(ScoreVersion, { foreignKey: 'examId', as: 'scoreVersions' });
ScoreVersion.belongsTo(Exam, { foreignKey: 'examId' });

Exam.hasMany(Submission, { foreignKey: 'examId' });
Submission.belongsTo(Exam, { foreignKey: 'examId' });
Submission.belongsTo(ScoreVersion, { foreignKey: 'scoreVersionId' });

Exam.hasMany(WrongQuestion, { foreignKey: 'examId' });
WrongQuestion.belongsTo(Exam, { foreignKey: 'examId' });
PaperQuestion.hasMany(WrongQuestion, { foreignKey: 'paperQuestionId' });
WrongQuestion.belongsTo(PaperQuestion, { foreignKey: 'paperQuestionId' });

Paper.hasMany(Certificate, { foreignKey: 'paperId' });
Exam.hasOne(Certificate, { foreignKey: 'examId' });
Certificate.belongsTo(Exam, { foreignKey: 'examId' });
Certificate.belongsTo(User, { foreignKey: 'userId' });
Certificate.belongsTo(Paper, { foreignKey: 'paperId' });

module.exports = {
  sequelize,
  Organization,
  User,
  Question,
  Paper,
  PaperQuestion,
  Exam,
  DailyAttemptCount,
  Submission,
  ScoreVersion,
  WrongQuestion,
  Certificate,
};
