#include "gridfusion/raycast.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace gridfusion {

namespace {
constexpr double kInf = std::numeric_limits<double>::infinity();
}  // namespace

void normalizeDirection(double dx, double dy, double& ux, double& uy) {
    const double m = std::hypot(dx, dy);
    if (m == 0.0) {
        ux = 1.0;
        uy = 0.0;
        return;
    }
    ux = dx / m;
    uy = dy / m;
    // Snap near-axis noise: a component below 1e-12 of the other one is
    // treated as exactly zero.
    constexpr double kAxisEps = 1e-12;
    if (std::fabs(uy) > 0.0 && std::fabs(ux) <= kAxisEps * std::fabs(uy))
        ux = 0.0;
    if (std::fabs(ux) > 0.0 && std::fabs(uy) <= kAxisEps * std::fabs(ux))
        uy = 0.0;
}

// Production traversal: Amanatides & Woo (1987) with deterministic handling
// of rays that pass exactly through a grid corner (both axes cross at the
// same parameter): the walker advances diagonally, i.e. the two cells only
// touched at a single corner point are not visited.
bool traceBeamAmanatidesWoo(double sx, double sy, double dx, double dy,
                            double travel, int width, int height,
                            double origin_x, double origin_y,
                            double resolution, bool no_return,
                            const EventSink& sink) {
    { double ux, uy; normalizeDirection(dx, dy, ux, uy); dx = ux; dy = uy; }
    const double inv = 1.0 / resolution;
    const double fx0 = (sx - origin_x) * inv;
    const double fy0 = (sy - origin_y) * inv;
    const double fxe = fx0 + dx * inv * travel;
    const double fye = fy0 + dy * inv * travel;

    int cx = static_cast<int>(std::floor(fx0));
    int cy = static_cast<int>(std::floor(fy0));
    const int cxe = static_cast<int>(std::floor(fxe));
    const int cye = static_cast<int>(std::floor(fye));
    const bool endpoint_inside =
        cxe >= 0 && cxe < width && cye >= 0 && cye < height;
    const bool end_is_hit = !no_return && endpoint_inside;
    const double eps = 1e-12 * std::max(1.0, travel);

    auto emit = [&](int x, int y, int occupied) {
        sink({y * width + x, x, y, occupied});
    };

    if (travel == 0.0) {
        // Zero-length returned beam: hit at the sensor cell.
        if (end_is_hit) emit(cx, cy, 1);
        return !endpoint_inside;
    }

    // Beam that never leaves the sensor cell.
    if (cx == cxe && cy == cye) {
        emit(cx, cy, end_is_hit ? 1 : 0);
        return !endpoint_inside;
    }

    // Sensor cell is always traversed free.
    emit(cx, cy, 0);

    const int step_x = (dx > 0.0) - (dx < 0.0);
    const int step_y = (dy > 0.0) - (dy < 0.0);
    double t_max_x = kInf;
    double t_max_y = kInf;
    // Cell-space direction magnitude per unit t: |d| / resolution.
    if (dx != 0.0) {
        const double boundary =
            dx > 0.0 ? cx + 1.0 : static_cast<double>(cx);
        t_max_x = (boundary - fx0) * resolution / dx;
    }
    if (dy != 0.0) {
        const double boundary =
            dy > 0.0 ? cy + 1.0 : static_cast<double>(cy);
        t_max_y = (boundary - fy0) * resolution / dy;
    }
    const double t_delta_x =
        dx != 0.0 ? resolution / std::fabs(dx) : kInf;
    const double t_delta_y =
        dy != 0.0 ? resolution / std::fabs(dy) : kInf;

    while (true) {
        const bool tie = (t_max_x != kInf && t_max_y != kInf &&
                          std::fabs(t_max_x - t_max_y) <= eps);
        const double t_next = std::min(t_max_x, t_max_y);

        // No more grid boundaries before the ray endpoint: the endpoint cell
        // (already entered and emitted on a previous crossing) is the last
        // event. Floating slack is absorbed by the boundary branch below.
        if (t_next > travel + eps) return endpoint_inside ? false : true;

        // Crossing exactly at the ray endpoint: next half-open cell is the
        // endpoint cell.
        if (std::fabs(t_next - travel) <= eps) {
            if (!endpoint_inside) return true;
            emit(cxe, cye, end_is_hit ? 1 : 0);
            return false;
        }

        // Interior crossing: which cell do we enter?
        int nx = cx, ny = cy;
        if (tie) {
            nx = cx + step_x;
            ny = cy + step_y;  // corner: skip the two side-adjacent cells
        } else if (t_max_x < t_max_y) {
            nx = cx + step_x;
        } else {
            ny = cy + step_y;
        }
        if (nx < 0 || nx >= width || ny < 0 || ny >= height)
            return true;  // ray leaves the map: in-map prefix was all free

        // Entering the endpoint cell: occupied for a returned beam, free at
        // the end of a no-return beam. Nothing is emitted twice.
        if (nx == cxe && ny == cye) {
            emit(nx, ny, end_is_hit ? 1 : 0);
            return false;
        }

        cx = nx;
        cy = ny;
        if (tie) {
            t_max_x += t_delta_x;
            t_max_y += t_delta_y;
        } else if (t_max_x < t_max_y) {
            t_max_x += t_delta_x;
        } else {
            t_max_y += t_delta_y;
        }
        emit(cx, cy, 0);
    }
}

// Independent reference traversal.
//
// Method (intentionally different from the production Amanatides-Woo DDA):
// build the explicit list of analytic grid-boundary crossing events along
// the ray, sort them by their ray parameter t, merge simultaneous x/y
// crossings (a ray passing exactly through a grid corner goes diagonally and
// does not visit the two cells touched only at that point), then replay the
// ordered event list. The geometric convention at corners/endpoints is thus
// stated in one place and shared, but the machinery (event table + sort, no
// incremental integer walk) is independent, so a coding mistake in one is
// unlikely to be mirrored in the other.
bool traceBeamSampled(double sx, double sy, double dx, double dy,
                      double travel, int width, int height,
                      double origin_x, double origin_y, double resolution,
                      bool no_return, const EventSink& sink) {
    { double ux, uy; normalizeDirection(dx, dy, ux, uy); dx = ux; dy = uy; }
    const double inv = 1.0 / resolution;
    const double fx0 = (sx - origin_x) * inv;
    const double fy0 = (sy - origin_y) * inv;
    const double fxe = fx0 + dx * inv * travel;
    const double fye = fy0 + dy * inv * travel;

    const int cx = static_cast<int>(std::floor(fx0));
    const int cy = static_cast<int>(std::floor(fy0));
    const int cxe = static_cast<int>(std::floor(fxe));
    const int cye = static_cast<int>(std::floor(fye));
    const bool endpoint_inside =
        cxe >= 0 && cxe < width && cye >= 0 && cye < height;
    const bool end_is_hit = !no_return && endpoint_inside;
    const double eps = 1e-12 * std::max(1.0, travel);

    struct Event { double t; int ax; };  // ax: 0 = x boundary, 1 = y
    std::vector<Event> events;

    auto pushAxis = [&](double f0, double fe, int axis) {
        const int i0 = static_cast<int>(std::floor(f0));
        const int ie = static_cast<int>(std::floor(fe));
        const double d = fe - f0;
        if (d == 0.0) return;
        const int lo = std::min(i0, ie) + 1;
        const int hi = std::max(i0, ie);  // crossing boundaries b in [lo,hi]
        for (int b = lo; b <= hi; ++b) {
            const double t = (static_cast<double>(b) - f0) / d * travel;
            if (t > -eps && t < travel + eps) events.push_back({t, axis});
        }
    };
    pushAxis(fx0, fxe, 0);
    pushAxis(fy0, fye, 1);

    std::sort(events.begin(), events.end(),
              [](const Event& a, const Event& b) { return a.t < b.t; });

    auto emit = [&](int x, int y, int occupied) -> bool {
        if (x < 0 || x >= width || y < 0 || y >= height)
            return true;  // stepped out of the map
        sink({y * width + x, x, y, occupied});
        return false;
    };

    if (travel == 0.0) {
        if (end_is_hit) emit(cx, cy, 1);
        return !endpoint_inside;
    }

    int gx = cx, gy = cy;
    const int stepx = (dx > 0.0) - (dx < 0.0);
    const int stepy = (dy > 0.0) - (dy < 0.0);

    // Endpoint lies inside the sensor cell (no boundary crossed).
    if (gx == cxe && gy == cye) {
        emit(gx, gy, end_is_hit ? 1 : 0);
        return !endpoint_inside;
    }
    if (emit(gx, gy, 0)) return true;  // sensor cell, free

    for (std::size_t i = 0; i < events.size();) {
        const double t = events[i].t;
        // Determine which axes cross within the corner tolerance.
        bool cxn = false, cyn = false;
        std::size_t j = i;
        while (j < events.size() &&
               std::fabs(events[j].t - t) <= eps) {
            if (events[j].ax == 0) cxn = true; else cyn = true;
            ++j;
        }
        const bool at_end = std::fabs(t - travel) <= eps;

        int nx = gx + (cxn ? stepx : 0);
        int ny = gy + (cyn ? stepy : 0);

        if (at_end) {
            // Endpoint on a boundary: half-open rule puts it in the cell we
            // are about to enter.
            if (!endpoint_inside) return true;
            emit(nx, ny, end_is_hit ? 1 : 0);
            return false;
        }

        if (nx == cxe && ny == cye) {
            if (emit(nx, ny, end_is_hit ? 1 : 0)) return true;
            return false;
        }
        if (emit(nx, ny, 0)) return true;
        gx = nx;
        gy = ny;
        i = j;
    }

    // No boundary at the endpoint (interior endpoint): the last entered cell
    // is the endpoint cell; safety net for floating slack.
    if (!endpoint_inside) return true;
    if (gx != cxe || gy != cye)
        if (emit(cxe, cye, end_is_hit ? 1 : 0)) return true;
    return false;
}

}  // namespace gridfusion
