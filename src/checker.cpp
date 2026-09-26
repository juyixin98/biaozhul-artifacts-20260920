#include "checker.hpp"

namespace sat {

namespace {

// 2 = satisfied, 1 = unit (unit_lit set), 0 = conflict, -1 = open.
int clause_state(const Clause& c, const std::vector<int8_t>& assignment,
                 Literal& unit_lit) {
  int unassigned = 0;
  Literal last = 0;
  for (Literal lit : c.literals) {
    int v = literal_value(assignment, lit);
    if (v == 1) return 2;
    if (v == -1) {
      ++unassigned;
      last = lit;
    }
  }
  if (unassigned == 0) return 0;
  if (unassigned == 1) {
    unit_lit = last;
    return 1;
  }
  return -1;
}

CheckResult fail(const std::string& message, size_t event_index) {
  return CheckResult{false,
                     "event " + std::to_string(event_index) + ": " + message};
}

}  // namespace

CheckResult check_assignment(const Formula& formula,
                             const std::vector<int8_t>& assignment) {
  if (assignment.size() != static_cast<size_t>(formula.num_vars) + 1) {
    return CheckResult{false, "assignment size does not match num_vars"};
  }
  for (size_t i = 0; i < formula.clauses.size(); ++i) {
    bool satisfied = false;
    for (Literal lit : formula.clauses[i].literals) {
      if (literal_value(assignment, lit) == 1) {
        satisfied = true;
        break;
      }
    }
    if (!satisfied) {
      return CheckResult{false,
                         "clause " + std::to_string(i) + " is not satisfied"};
    }
  }
  return CheckResult{true, ""};
}

CheckResult verify_proof(const Formula& formula,
                         const std::vector<Event>& events, Status claimed) {
  std::vector<int8_t> assignment(static_cast<size_t>(formula.num_vars) + 1, -1);

  // Assignments made under the current decision level, so a backtrack can
  // undo exactly them.
  struct Frame {
    int var = 0;
    bool tried_false = false;
    bool tried_true = false;
    std::vector<int> vars;
  };
  std::vector<Frame> stack;
  std::vector<int> level0_vars;

  // Record of the most recently popped frame; valid only until the next
  // decide. Because backtracks unwind strictly outward, this single slot
  // always describes the frame at the depth where the next decide happens.
  bool popped_valid = false;
  int popped_var = 0;
  bool popped_false = false;
  bool popped_true = false;

  bool unwinding = false;  // a conflict happened, backtracks expected
  bool saw_decide = false;
  bool saw_level0_conflict = false;

  for (size_t i = 0; i < events.size(); ++i) {
    const Event& e = events[i];
    switch (e.kind) {
      case Event::Kind::kDecide: {
        // Note: a decide may legitimately follow a backtrack chain. A
        // decide that ignored a conflict is still caught below, because
        // the falsified clause is still on the trail.
        if (e.var < 1 || e.var > formula.num_vars) {
          return fail("decide variable out of range", i);
        }
        if (assignment[static_cast<size_t>(e.var)] != -1) {
          return fail("decide on an already assigned variable", i);
        }
        if (all_clauses_satisfied(formula, assignment)) {
          return fail("decide although all clauses are satisfied", i);
        }
        for (const Clause& c : formula.clauses) {
          Literal unit = 0;
          int state = clause_state(c, assignment, unit);
          if (state == 0) return fail("decide while a clause is falsified", i);
          if (state == 1) return fail("decide while a unit clause is pending", i);
        }
        saw_decide = true;
        Frame frame;
        frame.var = e.var;
        if (popped_valid && popped_false && popped_true) {
          return fail("both values already explored at this level", i);
        }
        if (popped_valid && popped_false) {
          // Second attempt at this level: must be the same variable, true.
          if (e.value && e.var == popped_var) {
            frame.tried_false = true;
            frame.tried_true = true;
          } else {
            return fail("second branch must be true on the same variable", i);
          }
        } else {
          if (e.value != false) {
            return fail("first branch of a variable must be false", i);
          }
          int smallest = 0;
          for (int v = 1; v <= formula.num_vars; ++v) {
            if (assignment[static_cast<size_t>(v)] == -1) {
              smallest = v;
              break;
            }
          }
          if (e.var != smallest) {
            return fail("decide must pick the lowest unassigned variable", i);
          }
          frame.tried_false = true;
        }
        frame.vars.push_back(e.var);
        assignment[static_cast<size_t>(e.var)] = e.value ? 1 : 0;
        stack.push_back(std::move(frame));
        popped_valid = false;
        unwinding = false;
        break;
      }
      case Event::Kind::kUnit: {
        if (unwinding) return fail("unit propagation after conflict", i);
        if (e.clause < 0 ||
            e.clause >= static_cast<int>(formula.clauses.size())) {
          return fail("unit clause index out of range", i);
        }
        Literal unit = 0;
        int state = clause_state(formula.clauses[static_cast<size_t>(e.clause)],
                                 assignment, unit);
        if (state != 1) {
          return fail("clause is not unit under the current assignment", i);
        }
        if (unit != e.lit) {
          return fail("unit literal does not match the clause", i);
        }
        int var = e.lit < 0 ? -e.lit : e.lit;
        assignment[static_cast<size_t>(var)] = e.lit > 0 ? 1 : 0;
        if (stack.empty()) {
          level0_vars.push_back(var);
        } else {
          stack.back().vars.push_back(var);
        }
        break;
      }
      case Event::Kind::kConflict: {
        if (unwinding) return fail("conflict reported twice in a row", i);
        if (e.clause < 0 ||
            e.clause >= static_cast<int>(formula.clauses.size())) {
          return fail("conflict clause index out of range", i);
        }
        Literal unit = 0;
        int state = clause_state(formula.clauses[static_cast<size_t>(e.clause)],
                                 assignment, unit);
        if (state != 0) {
          return fail("conflict clause is not falsified", i);
        }
        unwinding = true;
        if (stack.empty()) saw_level0_conflict = true;
        break;
      }
      case Event::Kind::kBacktrack: {
        if (!unwinding) return fail("backtrack without a preceding conflict", i);
        if (stack.empty()) return fail("backtrack below decision level 0", i);
        Frame frame = std::move(stack.back());
        stack.pop_back();
        if (frame.var != e.var) {
          return fail("backtrack variable does not match the open decision", i);
        }
        for (int var : frame.vars) assignment[static_cast<size_t>(var)] = -1;
        popped_valid = true;
        popped_var = frame.var;
        popped_false = frame.tried_false;
        popped_true = frame.tried_true;
        break;
      }
    }
  }

  if (claimed == Status::kSat) {
    if (unwinding) {
      return CheckResult{false, "trace ends in a conflict but claims sat"};
    }
    if (!all_clauses_satisfied(formula, assignment)) {
      return CheckResult{false,
                         "final assignment does not satisfy all clauses"};
    }
    return CheckResult{true, ""};
  }

  // Claimed UNSAT: the whole tree must have been explored.
  if (!stack.empty()) {
    return CheckResult{false, "unsat claimed with open decisions"};
  }
  if (saw_decide) {
    if (!popped_valid || !popped_false || !popped_true) {
      return CheckResult{false,
                         "unsat claimed but the root variable was not "
                         "explored in both directions"};
    }
  } else if (!saw_level0_conflict) {
    return CheckResult{false, "unsat claimed without any conflict"};
  }
  return CheckResult{true, ""};
}

}  // namespace sat
