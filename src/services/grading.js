const { sameAnswerSet, canonicalJson, sha256 } = require('../utils/time');

// Normalize a client-given answer value to a sorted string array.
function normalizeGiven(value) {
  if (value == null) return [];
  const list = Array.isArray(value) ? value : [value];
  return [...new Set(list.map((v) => String(v)).filter((v) => v !== ''))].sort();
}

// Accept answers keyed by paper-question position ("1".."20") or by
// paperQuestionId. The two namespaces can collide numerically (ids 1..20
// look just like positions 1..20), so they are kept in separate maps:
//   - plain numbers ("1")            -> position (primary documented form)
//   - prefixed keys ("position:1",
//     "p:1", "question:123", "q:123") -> explicit namespace
// Values may be a single string or an array.
function indexAnswers(answersPayload) {
  const byPosition = new Map();
  const byPqId = new Map();
  if (!answersPayload || typeof answersPayload !== 'object') return { byPosition, byPqId };

  const put = (key, rawValue) => {
    const norm = normalizeGiven(rawValue);
    const k = String(key);
    const posMatch = k.match(/^(?:p(?:osition)?:)?(\d+)$/i);
    const qMatch = k.match(/^q(?:uestion)?:(\d+)$/i);
    if (qMatch) byPqId.set(qMatch[1], norm);
    else if (posMatch) byPosition.set(posMatch[1], norm);
  };

  if (Array.isArray(answersPayload)) {
    for (const entry of answersPayload) {
      put(entry.position != null ? `position:${entry.position}` : `question:${entry.paperQuestionId ?? entry.id}`, entry.answer);
    }
  } else {
    for (const [key, value] of Object.entries(answersPayload)) put(key, value);
  }
  return { byPosition, byPqId };
}

// Server-side grading against the FROZEN snapshot. Rule "exact": every
// question type (multiple included) earns points only when the selected
// option set equals the answer set completely — no partial credit.
function gradePaper(paperQuestions, answersPayload) {
  const { byPosition, byPqId } = indexAnswers(answersPayload);
  const detail = [];
  let correctCount = 0;

  for (const pq of paperQuestions) {
    const given = byPosition.get(String(pq.position)) || byPqId.get(String(pq.id)) || [];
    const correct = sameAnswerSet(given, pq.answer);
    if (correct) correctCount += 1;
    detail.push({
      paperQuestionId: pq.id,
      position: pq.position,
      type: pq.type,
      difficulty: pq.difficulty,
      givenAnswer: given,
      correctAnswer: pq.answer, // only ever returned AFTER grading
      correct,
      pointsEarned: correct ? pq.points : 0,
      points: pq.points,
      explanation: pq.explanation,
    });
  }

  const maxScore = paperQuestions.reduce((s, q) => s + q.points, 0);
  const earned = detail.reduce((s, d) => s + d.pointsEarned, 0);
  const score = maxScore === 0 ? 0 : Math.round((earned / maxScore) * 100);
  return { score, correctCount, wrongCount: paperQuestions.length - correctCount, detail };
}

function hashAnswers(answersPayload) {
  return sha256(canonicalJson(answersPayload ?? null));
}

module.exports = { gradePaper, hashAnswers, normalizeGiven, indexAnswers };
