#include "app/query.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <vector>

#include "geo/bvh.h"
#include "geo/ray_box.h"
#include "geo/types.h"

using js::JValue;

namespace app {

namespace {

constexpr double DEFAULT_EPS = 1e-12;

// —— 读取辅助：错误信息直接定位到字段路径 ——

bool readVec3(const JValue* parent, const char* key, geo::Vec3& out,
              std::string& err) {
    const JValue* v = parent->find(key);
    if (!v) {
        err = std::string("缺少字段 '") + key + "'";
        return false;
    }
    if (!v->isArray() || v->asArray().size() != 3) {
        err = std::string("字段 '") + key + "' 必须是含 3 个数的数组 [x,y,z]";
        return false;
    }
    double comps[3];
    for (int i = 0; i < 3; ++i) {
        const JValue& c = v->asArray()[i];
        if (!c.isNumber()) {
            err = std::string("字段 '") + key + "' 的分量必须是数字";
            return false;
        }
        comps[i] = c.asNumber();
        if (!std::isfinite(comps[i])) {
            err = std::string("字段 '") + key + "' 含非有限数值（NaN/无穷大不允许）";
            return false;
        }
    }
    out = geo::Vec3(comps[0], comps[1], comps[2]);
    return true;
}

// 从 JSON 数字提取 int64 id；必须确为整数且在范围内。
bool readInt64(const JValue& v, int64_t& out) {
    if (!v.isNumber()) return false;
    double d = v.asNumber();
    if (!std::isfinite(d)) return false;
    if (d != std::floor(d)) return false;
    if (d < static_cast<double>(std::numeric_limits<int64_t>::min()) ||
        d > static_cast<double>(std::numeric_limits<int64_t>::max())) {
        return false;
    }
    out = static_cast<int64_t>(d);
    return true;
}

JValue vec3ToJson(const geo::Vec3& v) {
    JValue a = JValue::makeArray();
    a.push(JValue::makeNumber(v.x));
    a.push(JValue::makeNumber(v.y));
    a.push(JValue::makeNumber(v.z));
    return a;
}

JValue hitToJson(const geo::Hit& h) {
    JValue o = JValue::makeObject();
    o.set("id", JValue::makeInt(h.id));
    o.set("t_enter", JValue::makeNumber(h.tEnter));
    o.set("t_exit", JValue::makeNumber(h.tExit));
    o.set("inside", JValue::makeBool(h.inside));
    o.set("point_enter", vec3ToJson(h.pointEnter));
    o.set("point_exit", vec3ToJson(h.pointExit));
    return o;
}

JValue statsToJson(const geo::TraversalStats& s) {
    JValue o = JValue::makeObject();
    o.set("nodes_visited", JValue::makeInt(s.nodesVisited));
    o.set("boxes_tested", JValue::makeInt(s.boxesTested));
    return o;
}

// 命中结果逐字段对比（BVH vs 逐盒），返回不一致的描述；一致返回空串。
std::string compareHitLists(const std::vector<geo::Hit>& a,
                            const std::vector<geo::Hit>& b,
                            double eps) {
    if (a.size() != b.size()) {
        return "命中数量不一致：BVH=" + std::to_string(a.size()) +
               " 逐盒=" + std::to_string(b.size());
    }
    auto close = [&](double x, double y) {
        const double tol = eps * std::max({1.0, std::fabs(x), std::fabs(y)});
        return std::fabs(x - y) <= tol;
    };
    for (size_t i = 0; i < a.size(); ++i) {
        const geo::Hit& x = a[i];
        const geo::Hit& y = b[i];
        if (x.id != y.id) return "第 " + std::to_string(i) + " 个命中 id 不一致";
        if (!close(x.tEnter, y.tEnter))
            return "第 " + std::to_string(i) + " 个命中 t_enter 不一致";
        if (!close(x.tExit, y.tExit))
            return "第 " + std::to_string(i) + " 个命中 t_exit 不一致";
        for (int c = 0; c < 3; ++c) {
            if (!close(x.pointEnter[c], y.pointEnter[c]))
                return "第 " + std::to_string(i) + " 个命中入射点坐标不一致";
            if (!close(x.pointExit[c], y.pointExit[c]))
                return "第 " + std::to_string(i) + " 个命中出射点坐标不一致";
        }
    }
    return "";
}

}  // namespace

std::string errorResponse(const std::string& message) {
    JValue o = JValue::makeObject();
    o.set("ok", JValue::makeBool(false));
    o.set("error", JValue::makeString(message));
    return o.dump();
}

std::string handleRequest(const std::string& requestText) {
    bool parsed = false;
    std::string perr;
    JValue root = js::parse(requestText, parsed, perr);
    if (!parsed) return errorResponse(perr);
    if (!root.isObject()) return errorResponse("请求根必须是 JSON 对象");

    std::string err;

    // mode
    const JValue* modeV = root.find("mode");
    if (!modeV || !modeV->isString()) {
        return errorResponse("缺少字符串字段 'mode'（nearest | all）");
    }
    const std::string& mode = modeV->asString();
    if (mode != "nearest" && mode != "all") {
        return errorResponse("mode 必须是 'nearest' 或 'all'");
    }

    // ray
    const JValue* rayV = root.find("ray");
    if (!rayV || !rayV->isObject()) {
        return errorResponse("缺少对象字段 'ray'（含 origin 与 direction）");
    }
    geo::Vec3 origin, dirRaw;
    if (!readVec3(rayV, "origin", origin, err)) return errorResponse(err);
    if (!readVec3(rayV, "direction", dirRaw, err)) return errorResponse(err);

    // eps（可选）
    double eps = DEFAULT_EPS;
    if (const JValue* e = root.find("eps")) {
        if (!e->isNumber() || !std::isfinite(e->asNumber()) ||
            e->asNumber() <= 0.0 || e->asNumber() >= 1.0) {
            return errorResponse("eps 必须是区间 (0, 1) 内的有限正数（建议 1e-12）");
        }
        eps = e->asNumber();
    }

    // boxes
    const JValue* boxesV = root.find("boxes");
    if (!boxesV || !boxesV->isArray()) {
        return errorResponse("缺少数组字段 'boxes'");
    }
    std::vector<geo::BoxInput> inputs;
    inputs.reserve(boxesV->asArray().size());
    std::set<int64_t> usedIds;
    for (size_t i = 0; i < boxesV->asArray().size(); ++i) {
        const JValue& bj = boxesV->asArray()[i];
        const std::string prefix =
            "boxes[" + std::to_string(i) + "] ";
        if (!bj.isObject()) return errorResponse(prefix + "必须是对象");
        geo::Vec3 mn, mx;
        if (const JValue* bmn = bj.find("min")) {
            if (!bmn->isArray() || bmn->asArray().size() != 3)
                return errorResponse(prefix + "min 必须是 [x,y,z]");
            double tmp[3];
            for (int k = 0; k < 3; ++k) {
                if (!bmn->asArray()[k].isNumber())
                    return errorResponse(prefix + "min 分量必须是数字");
                tmp[k] = bmn->asArray()[k].asNumber();
                if (!std::isfinite(tmp[k]))
                    return errorResponse(prefix + "min 含非有限数值");
            }
            mn = geo::Vec3(tmp[0], tmp[1], tmp[2]);
        } else {
            return errorResponse(prefix + "缺少 min");
        }
        if (const JValue* bmx = bj.find("max")) {
            if (!bmx->isArray() || bmx->asArray().size() != 3)
                return errorResponse(prefix + "max 必须是 [x,y,z]");
            double tmp[3];
            for (int k = 0; k < 3; ++k) {
                if (!bmx->asArray()[k].isNumber())
                    return errorResponse(prefix + "max 分量必须是数字");
                tmp[k] = bmx->asArray()[k].asNumber();
                if (!std::isfinite(tmp[k]))
                    return errorResponse(prefix + "max 含非有限数值");
            }
            mx = geo::Vec3(tmp[0], tmp[1], tmp[2]);
        } else {
            return errorResponse(prefix + "缺少 max");
        }
        for (int a = 0; a < 3; ++a) {
            if (!(mn[a] <= mx[a])) {
                return errorResponse(
                    prefix + "存在 min 分量大于 max（退化盒允许 min==max，"
                             "但不允许 min>max）");
            }
        }

        int64_t id = static_cast<int64_t>(i);  // 默认 id = 数组下标
        if (const JValue* idV = bj.find("id")) {
            if (!readInt64(*idV, id)) {
                return errorResponse(prefix + "id 必须是 int64 范围内的整数");
            }
            if (!usedIds.insert(id).second) {
                return errorResponse(prefix + "id 与其他盒重复");
            }
        }
        inputs.push_back({id, geo::AABB{mn, mx}});
    }

    geo::Ray ray;
    if (!geo::makeRay(origin, dirRaw, ray)) {
        return errorResponse(
            "射线方向为零向量或非有限：direction 必须是非零的有限向量");
    }

    geo::BVH bvh;
    bvh.build(inputs, eps);

    JValue resp = JValue::makeObject();
    resp.set("ok", JValue::makeBool(true));
    resp.set("mode", JValue::makeString(mode));
    resp.set("ray_origin", vec3ToJson(ray.origin));
    resp.set("ray_direction_unit", vec3ToJson(ray.dir));
    resp.set("box_count", JValue::makeInt(bvh.boxCount()));
    resp.set("bvh_node_count", JValue::makeInt(bvh.nodeCount()));
    resp.set("eps", JValue::makeNumber(eps));

    geo::TraversalStats st;
    bool bvhConsistent = true;
    std::string consistencyDetail = "match";

    if (mode == "nearest") {
        geo::Hit hit;
        const bool found = bvh.nearest(ray, hit, &st);
        resp.set("hit", JValue::makeBool(found));
        if (found) {
            resp.set("nearest", hitToJson(hit));

            // 与逐盒检测对照
            geo::Hit brute;
            const bool bf = bvh.bruteNearest(ray, brute);
            const double tol =
                eps * std::max({1.0, std::fabs(hit.tEnter),
                                std::fabs(brute.tEnter)});
            if (!bf || brute.id != hit.id ||
                std::fabs(brute.tEnter - hit.tEnter) > tol) {
                bvhConsistent = false;
                consistencyDetail = "nearest 与逐盒检测结果不一致";
            }
        } else {
            geo::Hit bruteMiss;
            if (bvh.bruteNearest(ray, bruteMiss)) {
                bvhConsistent = false;
                consistencyDetail = "BVH 报未命中但逐盒检测命中";
            }
        }
    } else {
        std::vector<geo::Hit> hits = bvh.allHits(ray, &st);
        std::vector<geo::Hit> brute = bvh.bruteAll(ray);
        JValue arr = JValue::makeArray();
        for (const auto& h : hits) arr.push(hitToJson(h));
        resp.set("hits", arr);
        resp.set("hit_count", JValue::makeInt(static_cast<int64_t>(hits.size())));

        std::string diff = compareHitLists(hits, brute, eps);
        if (!diff.empty()) {
            bvhConsistent = false;
            consistencyDetail = diff;
        }
    }

    resp.set("stats", statsToJson(st));
    JValue check = JValue::makeObject();
    check.set("method", JValue::makeString("bvh_vs_bruteforce"));
    check.set("consistent", JValue::makeBool(bvhConsistent));
    check.set("detail", JValue::makeString(consistencyDetail));
    resp.set("consistency_check", check);

    return resp.dump();
}

}  // namespace app
