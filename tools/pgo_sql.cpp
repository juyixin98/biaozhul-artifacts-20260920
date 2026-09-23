// pgo_sql — minimal read-only SQLite query tool for the run database.
//   pgo_sql tables DB                 list tables
//   pgo_sql runs DB [--status S]      dump runs (newest first)
//   pgo_sql edges DB RUN_ID [PHASE]   dump edge errors of a run
//   pgo_sql nodes DB RUN_ID           dump optimized nodes of a run
//   pgo_sql sql DB "SELECT ..."       run an arbitrary read-only statement
#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

#include <sqlite3.h>

namespace {

[[noreturn]] void die(const std::string& m) {
  std::cerr << "error: " << m << "\n";
  std::exit(1);
}

int runQuery(sqlite3* db, const std::string& sql) {
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(db, sql.c_str(), -1, &st, nullptr) != SQLITE_OK)
    die(std::string("prepare: ") + sqlite3_errmsg(db) + " [" + sql + "]");
  int ncol = sqlite3_column_count(st);
  for (int c = 0; c < ncol; ++c) {
    if (c) std::cout << "\t";
    std::cout << sqlite3_column_name(st, c);
  }
  if (ncol) std::cout << "\n";
  int rows = 0;
  while (sqlite3_step(st) == SQLITE_ROW) {
    for (int c = 0; c < ncol; ++c) {
      if (c) std::cout << "\t";
      const unsigned char* t = sqlite3_column_text(st, c);
      std::cout << (t ? reinterpret_cast<const char*>(t) : "");
    }
    std::cout << "\n";
    ++rows;
  }
  sqlite3_finalize(st);
  return rows;
}

sqlite3* openRo(const std::string& path) {
  sqlite3* db = nullptr;
  if (sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK)
    die("cannot open " + path);
  return db;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr <<
        "usage:\n"
        "  pgo_sql tables DB\n"
        "  pgo_sql runs DB [STATUS]\n"
        "  pgo_sql edges DB RUN_ID [before|after]\n"
        "  pgo_sql nodes DB RUN_ID\n"
        "  pgo_sql frozen DB\n"
        "  pgo_sql sql DB \"SELECT ...\"\n";
    return 2;
  }
  std::string cmd = argv[1];
  std::string path = argv[2];
  sqlite3* db = openRo(path);

  if (cmd == "tables") {
    runQuery(db,
             "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;");
  } else if (cmd == "runs") {
    if (argc >= 4) {
      sqlite3_stmt* st = nullptr;
      sqlite3_prepare_v2(db,
          "SELECT run_id,created_at,status,num_nodes,num_edges,num_components,"
          "termination,iterations,initial_chi2,final_chi2,initial_rms,final_rms,"
          "graph_sha256,cancel_reason FROM runs WHERE status=?1 "
          "ORDER BY created_at DESC, run_id DESC", -1, &st, nullptr);
      sqlite3_bind_text(st, 1, argv[3], -1, SQLITE_TRANSIENT);
      const char* names[] = {"run_id","created_at","status","num_nodes","num_edges",
          "num_components","termination","iterations","initial_chi2","final_chi2",
          "initial_rms","final_rms","graph_sha256","cancel_reason"};
      for (const char* n : names) std::cout << n << "\t";
      std::cout << "\n";
      while (sqlite3_step(st) == SQLITE_ROW) {
        for (int c = 0; c < 14; ++c) {
          if (c) std::cout << "\t";
          const unsigned char* t = sqlite3_column_text(st, c);
          std::cout << (t ? reinterpret_cast<const char*>(t) : "");
        }
        std::cout << "\n";
      }
      sqlite3_finalize(st);
    } else {
      runQuery(db,
          "SELECT run_id,created_at,status,num_nodes,num_edges,num_components,"
          "termination,iterations,initial_chi2,final_chi2,cancel_reason "
          "FROM runs ORDER BY created_at DESC, run_id DESC;");
    }
  } else if (cmd == "edges") {
    if (argc < 4) die("edges requires RUN_ID");
    std::string phase = argc >= 5 ? argv[4] : "after";
    sqlite3_stmt* st = nullptr;
    sqlite3_prepare_v2(db,
        "SELECT edge_id,node_from,node_to,round(e_x,6),round(e_y,6),round(e_theta,6),"
        "round(chi2,6),round(robust_weight,6),phase FROM edge_errors "
        "WHERE run_id=?1 AND phase=?2 ORDER BY edge_id", -1, &st, nullptr);
    sqlite3_bind_text(st, 1, argv[3], -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 2, phase.c_str(), -1, SQLITE_TRANSIENT);
    const char* hdr[] = {"edge","from","to","e_x","e_y","e_theta","chi2","weight","phase"};
    for (const char* h : hdr) std::cout << h << "\t";
    std::cout << "\n";
    while (sqlite3_step(st) == SQLITE_ROW) {
      for (int c = 0; c < 9; ++c) {
        if (c) std::cout << "\t";
        const unsigned char* t = sqlite3_column_text(st, c);
        std::cout << (t ? reinterpret_cast<const char*>(t) : "");
      }
      std::cout << "\n";
    }
    sqlite3_finalize(st);
  } else if (cmd == "nodes") {
    if (argc < 4) die("nodes requires RUN_ID");
    sqlite3_stmt* st = nullptr;
    sqlite3_prepare_v2(db,
        "SELECT node_id,round(x,6),round(y,6),round(theta,6) FROM run_nodes "
        "WHERE run_id=?1 ORDER BY ord", -1, &st, nullptr);
    sqlite3_bind_text(st, 1, argv[3], -1, SQLITE_TRANSIENT);
    std::cout << "node\tx\ty\ttheta\n";
    while (sqlite3_step(st) == SQLITE_ROW) {
      for (int c = 0; c < 4; ++c) {
        if (c) std::cout << "\t";
        const unsigned char* t = sqlite3_column_text(st, c);
        std::cout << (t ? reinterpret_cast<const char*>(t) : "");
      }
      std::cout << "\n";
    }
    sqlite3_finalize(st);
  } else if (cmd == "frozen") {
    runQuery(db, "SELECT name,sha256,created_at,length(raw_json) FROM frozen_graphs;");
  } else if (cmd == "sql") {
    if (argc < 4) die("sql requires a query");
    runQuery(db, argv[3]);
  } else {
    die("unknown command: " + cmd);
  }
  sqlite3_close(db);
  return 0;
}
