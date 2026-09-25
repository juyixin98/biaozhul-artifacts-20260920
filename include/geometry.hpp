// geometry.hpp - 2D 平面几何原语与凸多边形裁剪
//
// 坐标系: 右手二维笛卡尔平面, x 向右, y 向上。
// 内部计算一律使用 long double; 输入数值按 IEEE-754 double 解析,
// 输出默认以 17 位有效数字(double 的往返精度)序列化。
// 角度方向约定: CCW = 逆时针(数学正向), CW = 顺时针。
#pragma once

#include <string>
#include <vector>

namespace geom {

struct Point {
    long double x = 0.0L;
    long double y = 0.0L;
};

// 结果退化类别。凸多边形与简单多边形的交在拓扑上恒为凸集,
// 因此退化结果只可能是: 空 / 点 / 线段 / 多边形。
enum class ResultKind {
    Empty,    // 无交: 交集测度为零且无公共点
    Point,    // 退化为一个点(顶点接触等)
    Segment,  // 退化为一条线段(沿边重合但无面积等)
    Polygon,  // 有面积的多边形
};

struct ClipResult {
    ResultKind kind = ResultKind::Empty;
    std::vector<Point> vertices;  // 有序、已统一方向(CCW)、已去除连续共线点
    long double area = 0.0L;      // 有向面积的绝对值; 点/线段/空为 0
    int orientation = 0;          // +1=CCW(Polygon), 其余为 0
    bool inputSubjectReversed = false;  // 输入主体是否为 CW(被规范化为 CCW 输出)
    std::string warning;          // 非致命提示(如检测到细条 sliver)
};

struct Eps {
    // 线性绝对容差, 单位与坐标相同。默认 1e-9。
    long double linear = 1e-9L;
};

// ---- 基本原语 ----
long double cross(const Point& a, const Point& b, const Point& c);
long double signedArea(const std::vector<Point>& poly);
bool pointInConvexCCW(const Point& p, const std::vector<Point>& poly,
                      const Eps& eps);
// 点是否落在任意简单多边形内部或边界上(射线法)。
bool pointInPolygon(const Point& p, const std::vector<Point>& poly,
                    const Eps& eps);

// ---- 校验 ----
// 校验简单多边形: 顶点数、重复连续点、尖刺、非相邻边相交/接触、
// 相邻边除公共端点外的接触一律拒绝。返回 "" 表示通过。
std::string validateSimplePolygon(const std::vector<Point>& poly,
                                  const char* name, const Eps& eps);

// 校验裁剪多边形为严格凸多边形(允许 CW/CCW 输入, 内部规范化)。
// 返回 "" 表示通过。
std::string validateConvexClip(const std::vector<Point>& poly, const Eps& eps);

// ---- 裁剪 ----
// Sutherland-Hodgman: subject 必须为简单多边形, clip 必须为严格凸多边形。
// 成功返回 ""。可能返回:
//   RESULT_NOT_SIMPLE / RESULT_MULTIPLE_COMPONENTS:
//   裁剪输出经检测存在重叠边(主体为凹时理论上不产生断连, 此处为防御性检查)。
std::string clipPolygonByConvex(const std::vector<Point>& subjectIn,
                                const std::vector<Point>& clipIn,
                                const Eps& eps, ClipResult& out);

// 沿环去除连续重复点(含首尾循环), 再去除连续共线点。
std::vector<Point> simplifyRing(const std::vector<Point>& poly,
                                const Eps& eps);

const char* resultKindName(ResultKind k);

}  // namespace geom
