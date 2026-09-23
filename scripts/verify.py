#!/usr/bin/env python3
"""
验收脚本（只用 Python 标准库）：
  1) 第一页建立快照，翻到第 2 页后进行插入 / 删除 / 改排序键；
  2) 继续用旧游标翻完，校验同一快照下无重无漏（id 集合等于快照建立时的全集）；
  3) 重新开第一页（新快照），校验变更全部反映；
  4) 校验：换筛选复用游标 -> 400；篡改游标 -> 403；快照过期 -> 410。

用法：python3 scripts/verify.py [baseUrl] [ttlSeconds]
默认 http://localhost:18099。脚本不负责启动服务。
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18099"
TTL = int(sys.argv[2]) if len(sys.argv) > 2 else 3
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def request(method, path, body=None):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def walk(params, mutate_after_pages=0):
    """从第一页翻到末页，返回 (ids_in_order, pages, statuses)。"""
    ids, pages, statuses = [], [], []
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        path = "/api/items?" + urllib.parse.urlencode(p)
        status, body = request("GET", path)
        statuses.append(status)
        if status != 200:
            return ids, pages, statuses, body
        pages.append(body)
        ids.extend(it["id"] for it in body["items"])
        cursor = body.get("nextCursor")
        if not cursor:
            return ids, pages, statuses, None
        if mutate_after_pages and len(pages) == mutate_after_pages:
            mutate()


def mutate():
    print("  -- 翻页途中：插入 zzz-verifier / 删除 id=21 / 把 id=33 改名为 aaa-verifier")
    s, _ = request("POST", "/api/admin/items",
                   {"name": "zzz-verifier", "category": "books", "score": 123})
    check("插入返回 201", s == 201, f"实际 {s}")
    s, _ = request("DELETE", "/api/admin/items/21")
    check("删除 id=21 返回 200", s == 200, f"实际 {s}")
    s, _ = request("PATCH", "/api/admin/items/33", {"name": "aaa-verifier", "score": 1})
    check("修改 id=33 排序键返回 200", s == 200, f"实际 {s}")


def main():
    status, health = request("GET", "/healthz")
    check("健康检查 200", status == 200, str(health))
    total = request("GET", "/api/admin/stats")[1]["total"]
    print(f"  变更前真实数据总数 = {total}")

    params = {"sort": "name_asc", "pageSize": "7"}

    # ---- 场景一：翻页途中增删改，旧快照不重不漏 ----
    ids, pages, statuses, err = walk(params, mutate_after_pages=2)
    check("旧快照所有页 HTTP 200", all(s == 200 for s in statuses), str(statuses))
    check(f"旧快照遍历条数={total}（不重不漏）", len(ids) == total,
          f"实际 {len(ids)}")
    check("旧快照遍历无重复 id", len(set(ids)) == len(ids),
          f"唯一 {len(set(ids))} / 总 {len(ids)}")
    check("新插入记录不出现在旧快照", "zzz-verifier" not in
          [n for pg in pages for n in (it["name"] for it in pg["items"])]
          and max(ids) <= total)
    check("被删除的 id=21 仍由旧快照遍历到（快照已固定）", 21 in ids)
    check("被改名的 id=33 仍由旧快照遍历到（旧名字）", 33 in ids)
    old_snapshot = pages[0]["snapshotId"]
    check("全程绑定同一 snapshotId",
          all(pg["snapshotId"] == old_snapshot for pg in pages))

    # ---- 场景二：新快照反映全部变更 ----
    new_ids, new_pages, _, _ = walk(params)
    check(f"新快照条数 = {total}（删 1 增 1 净值不变）",
          len(new_ids) == total, f"实际 {len(new_ids)}")
    check("新快照不含已删除 id=21", 21 not in new_ids)
    check("新快照包含新插入记录",
          any(it["name"] == "zzz-verifier" for pg in new_pages for it in pg["items"]))
    first_new = new_pages[0]["items"][0]
    check("新快照第一条为改名后的 id=33 (aaa-verifier)",
          first_new["id"] == 33 and first_new["name"] == "aaa-verifier",
          str(first_new))
    check("新旧快照 ID 不同（快照确实重新建立）",
          new_pages[0]["snapshotId"] != old_snapshot)

    # ---- 场景三：变更筛选不能复用游标 ----
    _, books_pages, _, _ = walk({"sort": "name_asc", "category": "books", "pageSize": "5"})
    cursor = books_pages[0]["nextCursor"]
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "name_asc", "category": "movies", "pageSize": "5", "cursor": cursor}))
    check("换 category 复用游标 -> 400 cursor_query_mismatch",
          s == 400 and body["error"] == "cursor_query_mismatch", f"{s} {body}")
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "score_desc", "category": "books", "pageSize": "5", "cursor": cursor}))
    check("换 sort 复用游标 -> 400 cursor_query_mismatch",
          s == 400 and body["error"] == "cursor_query_mismatch", f"{s} {body}")
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "name_asc", "category": "books", "pageSize": "9", "cursor": cursor}))
    check("换 pageSize 复用游标 -> 400 cursor_query_mismatch",
          s == 400 and body["error"] == "cursor_query_mismatch", f"{s} {body}")

    # ---- 场景四：伪造/篡改游标 ----
    _, p, _, _ = walk({"sort": "name_asc", "pageSize": "5"})
    good = p[0]["nextCursor"]
    flipped = ("B" if good[0] == "A" else "A") + good[1:]
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "name_asc", "pageSize": "5", "cursor": flipped}))
    check("篡改一个字符 -> 403 cursor_invalid",
          s == 403 and body["error"] == "cursor_invalid", f"{s} {body}")
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "name_asc", "pageSize": "5", "cursor": "forged.cursor"}))
    check("完全伪造 -> 403 cursor_invalid",
          s == 403 and body["error"] == "cursor_invalid", f"{s} {body}")

    # ---- 场景五：快照过期明确报错 ----
    _, p, _, _ = walk({"sort": "name_asc", "pageSize": "5"})
    expire_cursor = p[0]["nextCursor"]
    print(f"  等待 {TTL + 1} 秒让快照过期（TTL={TTL}s）……")
    time.sleep(TTL + 1)
    s, body = request("GET", "/api/items?" + urllib.parse.urlencode(
        {"sort": "name_asc", "pageSize": "5", "cursor": expire_cursor}))
    check("过期游标 -> 410 snapshot_expired",
          s == 410 and body["error"] == "snapshot_expired", f"{s} {body}")

    print(f"\n验收结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
