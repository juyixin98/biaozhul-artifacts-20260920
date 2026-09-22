const { WrongQuestion, PaperQuestion, Exam } = require('../models');
const { notFound, forbidden } = require('../utils/errors');

// Wrong questions are stored with the SNAPSHOT version (paper_questions row)
// against which the attempt was graded. Joining the snapshot — never the
// live question bank — is what keeps review material stable when the bank
// changes. Latest score-version rows represent the current state.
async function listWrongQuestions({ user, examId = null }) {
  if (examId) {
    const exam = await Exam.findByPk(examId);
    if (!exam) throw notFound('exam not found');
    if (exam.userId !== user.id && user.role !== 'admin') throw forbidden();
    const rows = await WrongQuestion.findAll({
      where: { examId },
      include: [{ model: PaperQuestion }],
      order: [['scoreVersion', 'DESC'], ['paperQuestionId', 'ASC']],
    });
    return rows.map(serializeRow);
  }

  const rows = await WrongQuestion.findAll({
    where: { userId: user.id },
    include: [{ model: PaperQuestion }],
    order: [['createdAt', 'DESC']],
    limit: 200,
  });
  return rows.map(serializeRow);
}

function serializeRow(row) {
  const pq = row.PaperQuestion;
  return {
    examId: row.examId,
    scoreVersion: row.scoreVersion,
    paperQuestionId: row.paperQuestionId,
    position: pq ? pq.position : null,
    type: pq ? pq.type : null,
    // Frozen snapshot content:
    stem: pq ? pq.stem : null,
    options: pq ? pq.options : null,
    givenAnswer: row.givenAnswer,
    correctAnswer: row.correctAnswer,
    explanation: pq ? pq.explanation : null,
  };
}

module.exports = { listWrongQuestions };
