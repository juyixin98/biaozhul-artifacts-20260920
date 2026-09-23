#include "mesh.hpp"
#include "json.hpp"

#include <cmath>
#include <map>
#include <set>
#include <stdexcept>

namespace gridtopo {

using json::Value;

namespace {

[[noreturn]] void badRequest(const std::string& msg) {
    throw std::runtime_error(msg);
}

double asFiniteConfigNumber(const Value& v, const std::string& where) {
    if (!v.isNumber()) badRequest("config field " + where + " must be a number");
    if (!std::isfinite(v.numberValue) || v.numberValue <= 0.0) {
        badRequest("config field " + where + " must be a positive finite number");
    }
    return v.numberValue;
}

const Value* findField(const Value& obj, std::initializer_list<const char*> names) {
    for (const char* n : names) {
        if (obj.has(n)) return &obj.get(n);
    }
    return nullptr;
}

} // namespace

Mesh loadMesh(const Value& root) {
    if (!root.isObject()) badRequest("request root must be a JSON object");

    Mesh mesh;

    // ---- config（可选）----
    if (root.has("config")) {
        const Value& cfg = root.get("config");
        if (!cfg.isObject()) badRequest("config must be an object");
        if (cfg.has("coordinateSystem")) {
            const Value& v = cfg.get("coordinateSystem");
            if (!v.isString()) badRequest("config.coordinateSystem must be a string");
            mesh.config.coordinateSystem = v.stringValue;
        }
        if (cfg.has("lengthUnit")) {
            const Value& v = cfg.get("lengthUnit");
            if (!v.isString()) badRequest("config.lengthUnit must be a string");
            mesh.config.lengthUnit = v.stringValue;
        }
        if (cfg.has("eps")) {
            mesh.config.eps = asFiniteConfigNumber(cfg.get("eps"), "eps");
        }
        if (cfg.has("areaEps")) {
            mesh.config.areaEps = asFiniteConfigNumber(cfg.get("areaEps"), "areaEps");
        }
    }

    // ---- vertices ----
    const Value* verts = findField(root, {"vertices", "points"});
    if (!verts) badRequest("request must contain a 'vertices' array");
    if (!verts->isArray()) badRequest("vertices must be an array");

    mesh.vertices.reserve(verts->arrayValue.size());
    std::set<std::string> seenVertexIds;

    for (size_t i = 0; i < verts->arrayValue.size(); ++i) {
        const Value& vv = verts->arrayValue[i];
        Vertex vertex;
        vertex.id = "v" + std::to_string(i);

        const Value* coords = nullptr;
        if (vv.isArray()) {
            coords = &vv;
        } else if (vv.isObject()) {
            if (vv.has("id")) {
                const Value& idv = vv.get("id");
                if (!(idv.isString() || idv.isNumber())) badRequest("vertex id must be string or number");
                vertex.id = idv.isString() ? idv.stringValue : std::to_string(static_cast<long long>(idv.numberValue));
            }
            coords = findField(vv, {"coordinates", "position", "xyz", "point"});
            if (!coords) badRequest("vertex object at index " + std::to_string(i) + " lacks 'coordinates'");
        } else {
            badRequest("vertex at index " + std::to_string(i) + " must be [x,y,z] or object");
        }

        if (!coords->isArray() || coords->arrayValue.size() != 3) {
            badRequest("vertex '" + vertex.id + "' coordinates must be an array of 3 numbers");
        }
        for (int k = 0; k < 3; ++k) {
            const Value& c = coords->arrayValue[k];
            if (!c.isNumber()) badRequest("vertex '" + vertex.id + "' coordinates must be numbers");
            vertex.xyz[k] = c.numberValue;
        }
        if (!seenVertexIds.insert(vertex.id).second) {
            badRequest("duplicate vertex id '" + vertex.id + "'");
        }
        mesh.vertices.push_back(std::move(vertex));
    }

    // ---- faces ----
    const Value* faces = findField(root, {"faces", "triangles"});
    if (!faces) badRequest("request must contain a 'faces' array");
    if (!faces->isArray()) badRequest("faces must be an array");

    mesh.faces.reserve(faces->arrayValue.size());
    std::set<std::string> seenFaceIds;
    std::map<std::string, int> idToIndex;
    for (int i = 0; i < static_cast<int>(mesh.vertices.size()); ++i) {
        idToIndex[mesh.vertices[i].id] = i;
    }

    for (size_t fi = 0; fi < faces->arrayValue.size(); ++fi) {
        const Value& fv = faces->arrayValue[fi];
        Face face;
        face.id = "f" + std::to_string(fi);

        const Value* refs = nullptr;
        if (fv.isArray()) {
            refs = &fv;
        } else if (fv.isObject()) {
            if (fv.has("id")) {
                const Value& idv = fv.get("id");
                if (!(idv.isString() || idv.isNumber())) badRequest("face id must be string or number");
                face.id = idv.isString() ? idv.stringValue : std::to_string(static_cast<long long>(idv.numberValue));
            }
            refs = findField(fv, {"vertices", "indices", "triangle"});
            if (!refs) badRequest("face object at index " + std::to_string(fi) + " lacks 'vertices'");
        } else {
            badRequest("face at index " + std::to_string(fi) + " must be [i,j,k] or object");
        }

        if (!refs->isArray() || refs->arrayValue.size() != 3) {
            badRequest("face '" + face.id + "' must reference exactly 3 vertices");
        }
        for (int k = 0; k < 3; ++k) {
            const Value& r = refs->arrayValue[k];
            if (r.isString()) {
                auto it = idToIndex.find(r.stringValue);
                if (it == idToIndex.end()) {
                    badRequest("face '" + face.id + "' references unknown vertex id '" + r.stringValue + "'");
                }
                face.v[k] = it->second;
            } else if (r.isNumber()) {
                double d = r.numberValue;
                long long idx;
                if (d < 0 || d != static_cast<double>(static_cast<long long>(d))) {
                    badRequest("face '" + face.id + "' has invalid vertex index");
                }
                idx = static_cast<long long>(d);
                if (idx >= static_cast<long long>(mesh.vertices.size())) {
                    badRequest("face '" + face.id + "' references vertex index " + std::to_string(idx) + " out of range");
                }
                face.v[k] = static_cast<int>(idx);
            } else {
                badRequest("face '" + face.id + "' vertex references must be string ids or integer indices");
            }
        }
        if (!seenFaceIds.insert(face.id).second) {
            badRequest("duplicate face id '" + face.id + "'");
        }
        mesh.faces.push_back(std::move(face));
    }

    return mesh;
}

} // namespace gridtopo
