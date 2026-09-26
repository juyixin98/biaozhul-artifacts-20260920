#include "dpll.hpp"

namespace sat {

namespace {

enum class ClauseState { kSatisfied, kConflict, kUnit, kOpen };

ClauseState eval_clause(const Clause& c, const std::vector<int8_t>& assignment,
                        Literal& unit_lit) {
  int unassigned = 0;
  Literal last_unassigned = 0;
  for (Literal lit : c.literals) {
    int v = literal_value(assignment, lit);
    if (v == 1) return ClauseState::kSatisfied;
    if (v == -1) {
      ++unassigned;
      last_unassigned = lit;
    }
  }
  if (unassigned == 0) return ClauseState::kConflict;
  if (unassigned == 1) {
    unit_lit = last_unassigned;
    return ClauseState::kUnit;
  }
  return ClauseState::kOpen;
}

class Solver {
 public:
  Solver(const Formula& f, size_t max_events)
      : f_(f), max_events_(max_events) {
    assign_.assign(static_cast<size_t>(f.num_vars) + 1, -1);
  }

  SolveResult run() {
    SolveResult result;
    result.status = search();
    if (result.status == Status::kSat) result.assignment = assign_;
    result.events = std::move(events_);
    return result;
  }

 private:
  const Formula& f_;
  size_t max_events_;
  std::vector<int8_t> assign_;  // index 1..num_vars
  std::vector<int> trail_;      // assigned vars, in order (for undo)
  std::vector<Event> events_;

  void emit(Event e) {
    if (events_.size() >= max_events_) {
      throw LimitExceeded("search event limit exceeded (" +
                          std::to_string(max_events_) + ")");
    }
    events_.push_back(e);
  }

  void set(int var, bool value) {
    assign_[static_cast<size_t>(var)] = value ? 1 : 0;
    trail_.push_back(var);
  }

  void undo_to(size_t mark) {
    while (trail_.size() > mark) {
      assign_[static_cast<size_t>(trail_.back())] = -1;
      trail_.pop_back();
    }
  }

  int find_conflict() const {
    for (size_t i = 0; i < f_.clauses.size(); ++i) {
      Literal dummy = 0;
      if (eval_clause(f_.clauses[i], assign_, dummy) == ClauseState::kConflict) {
        return static_cast<int>(i);
      }
    }
    return -1;
  }

  bool find_unit(int& clause_index, Literal& unit_lit) const {
    for (size_t i = 0; i < f_.clauses.size(); ++i) {
      Literal lit = 0;
      if (eval_clause(f_.clauses[i], assign_, lit) == ClauseState::kUnit) {
        clause_index = static_cast<int>(i);
        unit_lit = lit;
        return true;
      }
    }
    return false;
  }

  int smallest_unassigned_var() const {
    for (int v = 1; v <= f_.num_vars; ++v) {
      if (assign_[static_cast<size_t>(v)] == -1) return v;
    }
    return 0;
  }

  Status search() {
    const size_t level_mark = trail_.size();

    // Unit propagation to fixpoint. Conflict is checked first so a
    // falsified clause is reported before any further propagation.
    while (true) {
      int conflict = find_conflict();
      if (conflict >= 0) {
        Event e;
        e.kind = Event::Kind::kConflict;
        e.clause = conflict;
        emit(e);
        undo_to(level_mark);
        return Status::kUnsat;
      }
      int unit_clause = -1;
      Literal unit_lit = 0;
      if (!find_unit(unit_clause, unit_lit)) break;
      Event e;
      e.kind = Event::Kind::kUnit;
      e.lit = unit_lit;
      e.clause = unit_clause;
      emit(e);
      int var = unit_lit < 0 ? -unit_lit : unit_lit;
      set(var, unit_lit > 0);
    }

    if (all_clauses_satisfied(f_, assign_)) return Status::kSat;

    // Deterministic branching: lowest unassigned variable, false then true.
    const int var = smallest_unassigned_var();
    for (bool value : {false, true}) {
      Event e;
      e.kind = Event::Kind::kDecide;
      e.var = var;
      e.value = value;
      emit(e);
      const size_t decision_mark = trail_.size();
      set(var, value);
      if (search() == Status::kSat) return Status::kSat;
      undo_to(decision_mark);
      Event b;
      b.kind = Event::Kind::kBacktrack;
      b.var = var;
      emit(b);
    }
    undo_to(level_mark);
    return Status::kUnsat;
  }
};

}  // namespace

SolveResult dpll_solve(const Formula& formula, size_t max_events) {
  Solver solver(formula, max_events);
  return solver.run();
}

}  // namespace sat
