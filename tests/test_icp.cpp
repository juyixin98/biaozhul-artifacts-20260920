// test_icp.cpp — executable acceptance tests for the registration core and SHA-256.
#include "icp.h"
#include "json.h"
#include "sha256.h"

#include <cmath>
#include <cstdio>
#include <limits>
#include <random>
#include <string>
#include <vector>

using namespace pcr;

static int g_failed = 0;
static int g_passed = 0;

#define CHECK(cond)                                                                         \
    do {                                                                                    \
        if (cond) { ++g_passed; }                                                           \
        else {                                                                              \
            ++g_failed;                                                                     \
            std::fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);           \
        }                                                                                   \
    } while (0)

#define CHECK_NEAR(a, b, tol)                                                               \
    do {                                                                                    \
        double _a = (a), _b = (b), _t = (tol);                                             \
        if (std::abs(_a - _b) <= (_t)) { ++g_passed; }                                     \
        else {                                                                              \
            ++g_failed;                                                                     \
            std::fprintf(stderr, "FAIL %s:%d  |%.9g - %.9g| = %.3g > %.3g (%s)\n",         \
                         __FILE__, __LINE__, _a, _b, std::abs(_a - _b), _t, #a);           \
        }                                                                                   \
    } while (0)

// Rotation of angle a around axis (Rodrigues).
static Eigen::Matrix3d rotAxis(const Eigen::Vector3d& axis, double a) {
    Eigen::Vector3d k = axis.normalized();
    Eigen::Matrix3d K;
    K << 0, -k.z(), k.y(), k.z(), 0, -k.x(), -k.y(), k.x(), 0;
    return Eigen::Matrix3d::Identity() + std::sin(a) * K + (1 - std::cos(a)) * (K * K);
}

// Centered cube of points in [-scale/2, scale/2]^3, so rotation displacement
// stays small relative to the adaptive correspondence gate.
static std::vector<Eigen::Vector3d> boxPoints(int n, std::mt19937_64& rng, double scale = 1.0) {
    std::uniform_real_distribution<double> u(-scale / 2.0, scale / 2.0);
    std::vector<Eigen::Vector3d> v;
    v.reserve(n);
    for (int i = 0; i < n; ++i)
        v.emplace_back(u(rng), u(rng), u(rng));
    return v;
}

static std::vector<Eigen::Vector3d> transform(const std::vector<Eigen::Vector3d>& v,
                                              const Eigen::Matrix3d& R,
                                              const Eigen::Vector3d& t) {
    auto out = v;
    for (auto& p : out) p = R * p + t;
    return out;
}

static void checkRotation(const Eigen::Matrix3d& R, double tol = 1e-9) {
    double ortho = (R * R.transpose() - Eigen::Matrix3d::Identity()).norm();
    double det = R.determinant();
    CHECK_NEAR(ortho, 0.0, tol);
    CHECK_NEAR(det, 1.0, tol);
}

void test_sha256() {
    // FIPS 180-4 / NESSIE known answers.
    CHECK(Sha256::hex(Sha256::hash("")) ==
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(Sha256::hex(Sha256::hash("abc")) ==
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    std::string million(1000000, 'a');
    CHECK(Sha256::hex(Sha256::hash(million)) ==
          "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

void test_json_roundtrip() {
    std::string s = "{\"source\":[[1,2.5,-3],[0,0,0]],\"target\":[]}";
    Json j = Json::parse(s);
    CHECK(j.isObject());
    CHECK(j.at("source").asArray().size() == 2);
    CHECK(j.at("source").asArray()[0].asArray()[1].asNumber() == 2.5);
    Json back = Json::parse(j.dump());
    CHECK(back.at("source").asArray()[0].asArray()[2].asNumber() == -3.0);
    bool threw = false;
    try { Json::parse("{bad}"); } catch (...) { threw = true; }
    CHECK(threw);

    // Non-finite tokens are accepted at the JSON layer (Python/JS extension)
    // and must surface as non-finite numbers that cleanCloud() drops.
    Json ext = Json::parse("[NaN, Infinity, -Infinity, 1.0]");
    CHECK(ext.asArray().size() == 4);
    CHECK(std::isnan(ext.asArray()[0].asNumber()));
    CHECK(std::isinf(ext.asArray()[1].asNumber()));
    CHECK(ext.asArray()[2].asNumber() == -std::numeric_limits<double>::infinity());
}

void test_known_transform() {
    std::mt19937_64 rng(20260923ULL);
    auto base = boxPoints(400, rng, 2.0);
    Eigen::Matrix3d R = rotAxis(Eigen::Vector3d(0.3, 0.7, 0.2).normalized(), 0.35);
    Eigen::Vector3d t(0.15, -0.1, 0.08);
    auto moved = transform(base, R, t);

    PointCloud3 src = cleanCloud(moved);
    PointCloud3 tgt = cleanCloud(base);
    IcpOptions o100; o100.maxIterations = 100;
    IcpResult r = runIcp(src, tgt, o100);

    checkRotation(r.rotation);
    CHECK(r.converged);
    CHECK(r.reason == Reason::Converged);
    CHECK(r.highConfidence);
    CHECK(r.rmse < 1e-8);
    CHECK_NEAR(r.inlierRatio, 1.0, 1e-9);
    // Recovered pose: source = R*base+t maps back, so rotation ≈ R^T, translation ≈ -R^T t.
    CHECK((r.rotation - R.transpose()).norm() < 1e-7);
    CHECK((r.translation + R.transpose() * t).norm() < 1e-7);
}

void test_noisy_transform() {
    std::mt19937_64 rng(42ULL);
    auto base = boxPoints(500, rng, 2.0);
    Eigen::Matrix3d R = rotAxis(Eigen::Vector3d(1, 0, 0), 0.20);
    Eigen::Vector3d t(0.10, 0.05, 0.0);
    auto moved = transform(base, R, t);
    std::normal_distribution<double> noise(0.0, 0.002);
    for (auto& p : moved) p += Eigen::Vector3d(noise(rng), noise(rng), noise(rng));

    IcpOptions o100; o100.maxIterations = 100;
    IcpResult r = runIcp(cleanCloud(moved), cleanCloud(base), o100);
    checkRotation(r.rotation);
    CHECK(r.converged);
    // Reproducible error bound: noise sigma=0.002 per axis; RMSE must stay below 5 sigma.
    // Rotation/translation bounds follow the statistical scale sigma/(L sqrt(N))
    // with a several-sigma safety margin for fixed-seed reproducibility.
    CHECK(r.rmse < 5.0 * 0.002 * std::sqrt(3.0));
    CHECK((r.rotation - R.transpose()).norm() < 1e-3);
    CHECK((r.translation + R.transpose() * t).norm() < 1e-3);
}

void test_outliers() {
    std::mt19937_64 rng(7ULL);
    auto base = boxPoints(300, rng, 2.0);
    Eigen::Matrix3d R = rotAxis(Eigen::Vector3d(0, 0, 1), 0.25);
    Eigen::Vector3d t(0.2, 0.0, 0.0);
    auto moved = transform(base, R, t);

    // Inject 25% gross outliers (random points far away, mixed with NaN/Inf rows).
    std::uniform_real_distribution<double> u(-50.0, 50.0);
    std::vector<Eigen::Vector3d> withOutliers;
    for (size_t i = 0; i < moved.size(); ++i) {
        if (i % 4 == 0) withOutliers.emplace_back(u(rng), u(rng), u(rng));
        else withOutliers.push_back(moved[i]);
    }
    withOutliers.emplace_back(std::nan(""), 1.0, 2.0);
    withOutliers.emplace_back(0.0, std::numeric_limits<double>::infinity(), 1.0);

    PointCloud3 src = cleanCloud(withOutliers);
    CHECK(src.droppedNonFinite == 2);
    IcpOptions opts;
    opts.maxIterations = 100;
    opts.maxCorrespondenceDistance = 0.2;  // reject the gross outliers
    IcpResult r = runIcp(src, cleanCloud(base), opts);

    checkRotation(r.rotation);
    CHECK(r.converged);
    CHECK(r.rmse < 1e-6);
    // Inliers are the 225/300 good points; outliers must not be counted as inliers.
    CHECK(r.inlierRatio >= 0.70);
    CHECK(r.inlierRatio <= 0.80);
    CHECK((r.rotation - R.transpose()).norm() < 1e-5);
}

void test_collinear_degenerate() {
    // Both clouds are points on a single line: rotation about the line axis is
    // unobservable. ICP must not claim success/high confidence.
    std::vector<Eigen::Vector3d> line;
    for (int i = 0; i < 100; ++i) line.emplace_back(0.01 * i, 0.0, 0.0);
    Eigen::Matrix3d R = rotAxis(Eigen::Vector3d(1, 0, 0), 0.6);  // invisible about x
    auto moved = transform(line, R, Eigen::Vector3d(0.05, 0.0, 0.0));

    IcpOptions o100; o100.maxIterations = 100;
    IcpResult r = runIcp(cleanCloud(moved), cleanCloud(line), o100);
    checkRotation(r.rotation);
    CHECK(r.degenerate);
    CHECK(!r.highConfidence);
    CHECK(!r.converged);
    CHECK(r.reason == Reason::DegenerateGeometry);
}

void test_no_overlap() {
    std::mt19937_64 rng(11ULL);
    auto a = boxPoints(200, rng, 1.0);                    // around origin
    std::vector<Eigen::Vector3d> b;
    for (const auto& p : a) b.push_back(p + Eigen::Vector3d(100, 100, 100));  // far away

    IcpOptions opts;
    opts.maxIterations = 30;
    opts.maxCorrespondenceDistance = 0.5;
    IcpResult r = runIcp(cleanCloud(b), cleanCloud(a), opts);
    checkRotation(r.rotation, 1e-12);
    CHECK(!r.highConfidence);
    CHECK(!r.converged);
    CHECK(r.reason == Reason::NoOverlap);
    CHECK(r.correspondences == 0);
}

void test_initial_pose_and_identity() {
    std::mt19937_64 rng(99ULL);
    auto base = boxPoints(300, rng, 3.0);
    Eigen::Matrix3d R = rotAxis(Eigen::Vector3d(0.1, 0.2, 0.9).normalized(), 0.45);
    Eigen::Vector3d t(0.3, -0.2, 0.15);
    auto moved = transform(base, R, t);

    // Large misalignment with the default gate needs an initial guess.
    // Provide a deliberately imperfect guess (half the rotation, no translation);
    // it must be used only as an initialization — never replaced by truth.
    Eigen::Matrix3d Rhalf = rotAxis(Eigen::Vector3d(0.1, 0.2, 0.9).normalized(), 0.225);
    Eigen::Matrix4d init = Eigen::Matrix4d::Identity();
    init.topLeftCorner<3, 3>() = Rhalf.transpose();  // guess toward undoing
    IcpOptions o200; o200.maxIterations = 200;
    IcpResult r = runIcp(cleanCloud(moved), cleanCloud(base), o200, &init);
    checkRotation(r.rotation);
    CHECK(r.converged);
    CHECK((r.rotation - R.transpose()).norm() < 1e-6);
    CHECK((r.translation + R.transpose() * t).norm() < 1e-5);

    // Exact identity case: converged immediately, numerical residual at zero.
    IcpOptions o10; o10.maxIterations = 10;
    IcpResult same = runIcp(cleanCloud(base), cleanCloud(base), o10);
    checkRotation(same.rotation);
    CHECK(same.converged);
    CHECK(same.rmse < 1e-12);
}

void test_size_limits() {
    std::vector<Eigen::Vector3d> big(kMaxPoints + 1, Eigen::Vector3d::Zero());
    // The core's documented precondition is <=5000; API layer enforces it.
    // Here we verify tiny inputs are rejected rather than crashing.
    PointCloud3 tiny;
    tiny.points.resize(3, 2);
    tiny.points << 0, 1, 0, 0, 0, 0;
    PointCloud3 ok;
    ok.points.resize(3, 3);
    ok.points << 0, 1, 0, 0, 0, 1, 0, 0, 0;
    IcpResult r = runIcp(tiny, ok, {});
    CHECK(!r.ok);
    CHECK(r.reason == Reason::InvalidInput);
    (void)big;
}

int main() {
    test_sha256();
    test_json_roundtrip();
    test_known_transform();
    test_noisy_transform();
    test_outliers();
    test_collinear_degenerate();
    test_no_overlap();
    test_initial_pose_and_identity();
    test_size_limits();

    std::printf("\n%d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
