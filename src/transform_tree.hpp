// transform_tree.hpp —— TF 坐标树：静态/动态边分离管理与时间区间查询
#pragma once

#include "tf_math.hpp"

#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace tftree {

// 服务端错误。code 作为 JSON 的 "error" 字段，http_status 给出建议状态码。
struct Error : std::runtime_error {
    std::string code;
    int http_status;
    Error(std::string code_, std::string msg, int status)
        : std::runtime_error(std::move(msg)), code(std::move(code_)),
          http_status(status) {}
};

struct EdgeSample {
    std::string parent;
    std::string child;
    bool inverted = false;      // 查询路径中是否以逆方向经过该边
    bool is_static = false;     // 静态边 / 动态边
    double requested_time = 0;
    std::vector<double> sample_times;  // 使用到的 1 或 2 个样本时间（静态边为空）
    double time_error = 0.0;    // |requested_time - 使用样本时间| 的最大值（秒）
    std::string mode;           // exact | interpolated | static
};

struct QueryResult {
    Eigen::Vector3d translation;
    Eigen::Quaterniond rotation;
    Eigen::Matrix4d matrix = Eigen::Matrix4d::Identity();
    double time = 0.0;
    double max_time_error = 0.0;
    std::vector<EdgeSample> edges;
};

struct Edge {
    std::string parent;
    // 动态边的样本按时间排序；静态边使用 static_sample。
    bool is_static = false;
    std::optional<tfmath::Sample> static_sample;
    std::vector<tfmath::Sample> samples;
};

class TransformTree {
public:
    // 添加/覆盖一条静态边（父->子）。静态边可被同 parent->child 重复覆盖，
    // 但改变 child 的 parent 属于多父冲突，拒绝。
    void addStaticEdge(const std::string& parent, const std::string& child,
                       const Eigen::Vector3d& p, Eigen::Quaterniond q);

    // 为动态边 child 追加一个时间样本（边不存在时按 parent 创建）。
    // 同一时间戳的样本会被覆盖（异步乱序/重试用）。
    void addDynamicSample(const std::string& parent, const std::string& child,
                          const tfmath::Sample& sample);

    bool hasFrame(const std::string& f) const;

    // 查询 time 时刻 source -> target 的变换（点 p_target = T * p_source）。
    // boundary_tolerance: 查询时间在样本区间外但与端点相差 <= 该值时，
    // 吸附到端点样本（仍如实报告 time_error）。默认 0 = 禁止任何外推。
    QueryResult query(const std::string& source, const std::string& target,
                      double time, double boundary_tolerance = 0.0) const;

    // 供 /frames 接口输出当前树结构。
    struct FrameInfo {
        std::string name;
        std::string parent;  // 根为 ""
        bool is_static = false;
        std::size_t sample_count = 0;
        double t_min = 0, t_max = 0;
    };
    std::vector<FrameInfo> listFrames() const;

private:
    mutable std::mutex mu_;
    // child -> Edge（坐标树：每个子节点至多一个父节点）
    std::unordered_map<std::string, Edge> edges_;

    // 调用时持锁。
    void ensureNoMultiParentLocked(const std::string& child,
                                   const std::string& parent) const;
    void ensureNoCycleLocked(const std::string& parent,
                             const std::string& child) const;

    struct PathStep {
        std::string parent;
        std::string child;
        bool inverted;  // true: 查询沿 child->parent 走，需取逆
    };
    std::vector<PathStep> buildPathLocked(const std::string& source,
                                          const std::string& target) const;

    // 在一条边上求 time 时刻 父->子 的采样（内部已处理静态/插值/外推拒绝）。
    struct EdgeEval {
        tfmath::Sample sample;
        EdgeSample info;
    };
    EdgeEval evalEdgeLocked(const Edge& e, const std::string& parent,
                            const std::string& child, bool inverted,
                            double time, double boundary_tol) const;
};

}  // namespace tftree
