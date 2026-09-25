//
// test_app.cpp — tests for the JSON request layer (validation, degenerate
// handling, error responses, encoding round-trips).
//
#include "framework.hpp"

#include <string>

#include "../src/app.hpp"
#include "../src/json.hpp"

using ru::app::processRequest;
namespace j = ru::json;

namespace {

struct CallResult {
    int status;
    j::Value body;
};

CallResult call(const std::string& payload) {
    ru::app::Response r = processRequest(payload);
    return {r.status, j::parse(r.body)};
}

}  // namespace

TEST(App, SingleRect) {
    auto [status, body] = call(R"({"rectangles":[{"x1":0,"y1":0,"x2":2,"y2":2}]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 4);
    EXPECT_EQ(body.at("perimeter").asInt(), 8);
    EXPECT_EQ(body.at("rectangles_used").asInt(), 1);
    EXPECT_EQ(body.at("ignored_rectangles").arr().size(), 0u);
}

TEST(App, AdjacentPairNoDoubleCount) {
    auto [status, body] = call(
        R"({"rectangles":[{"x1":0,"y1":0,"x2":2,"y2":2},
                          {"x1":2,"y1":0,"x2":4,"y2":2}]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 8);
    EXPECT_EQ(body.at("perimeter").asInt(), 12);
}

TEST(App, NestedPair) {
    auto [status, body] = call(
        R"({"rectangles":[{"x1":0,"y1":0,"x2":4,"y2":4},
                          {"x1":1,"y1":1,"x2":3,"y2":3}]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 16);
    EXPECT_EQ(body.at("perimeter").asInt(), 16);
}

TEST(App, DegenerateReportedAndIgnored) {
    auto [status, body] = call(
        R"({"rectangles":[
             {"x1":0,"y1":0,"x2":2,"y2":2,"id":"real"},
             {"x1":2,"y1":0,"x2":2,"y2":2,"id":"vseg"},
             {"x1":0,"y1":2,"x2":2,"y2":2,"id":"hseg"},
             {"x1":3,"y1":3,"x2":3,"y2":3}
           ]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 4);
    EXPECT_EQ(body.at("perimeter").asInt(), 8);
    EXPECT_EQ(body.at("rectangles_in").asInt(), 4);
    EXPECT_EQ(body.at("rectangles_used").asInt(), 1);
    const auto& ig = body.at("ignored_rectangles").arr();
    ASSERT_EQ(ig.size(), 3u);
    EXPECT_EQ(ig[0].at("index").asInt(), 1);
    EXPECT_EQ(ig[0].at("id").asString(), "vseg");
    EXPECT_EQ(ig[1].at("id").asString(), "hseg");
    EXPECT_EQ(ig[2].at("index").asInt(), 3);
}

TEST(App, EmptyArray) {
    auto [status, body] = call(R"({"rectangles":[]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 0);
    EXPECT_EQ(body.at("perimeter").asInt(), 0);
}

TEST(App, ExtraFieldsIgnored) {
    auto [status, body] = call(
        R"({"note":"hello","rectangles":[{"x1":0,"y1":0,"x2":1,"y2":1,
           "color":"red","meta":{"k":"v"}}]})");
    EXPECT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 1);
}

TEST(App, InvalidJsonRejected) {
    auto [status, body] = call("{not json");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_JSON");
}

TEST(App, MissingRectangles) {
    auto [status, body] = call("{}");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_REQUEST");
}

TEST(App, RectanglesNotArray) {
    auto [status, body] = call(R"({"rectangles":{}})");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_REQUEST");
}

TEST(App, RectNotObject) {
    auto [status, body] = call(R"({"rectangles":[1,2]})");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_RECTANGLE");
}

TEST(App, MissingField) {
    auto [status, body] = call(R"({"rectangles":[{"x1":0,"y1":0,"x2":2}]})");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_RECTANGLE");
}

TEST(App, FractionRejected) {
    auto [status, body] = call(R"({"rectangles":[{"x1":0.5,"y1":0,"x2":2,"y2":2}]})");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_RECTANGLE");
}

TEST(App, StringCoordRejected) {
    auto [status, body] = call(R"({"rectangles":[{"x1":"0","y1":0,"x2":2,"y2":2}]})");
    EXPECT_EQ(status, 400);
}

TEST(App, InvertedCornersRejected) {
    auto [status, body] = call(R"({"rectangles":[{"x1":2,"y1":0,"x2":0,"y2":2}]})");
    EXPECT_EQ(status, 400);
    EXPECT_EQ(body.at("error").at("code").asString(), "INVALID_RECTANGLE");
}

TEST(App, OutOfRangeRejected) {
    auto [status, body] = call(R"({"rectangles":[{"x1":0,"y1":0,"x2":1000000001,"y2":2}]})");
    EXPECT_EQ(status, 400);
}

TEST(App, LargeValidCoordinates) {
    auto [status, body] = call(
        R"({"rectangles":[{"x1":-1000000000,"y1":-1000000000,
                           "x2":1000000000,"y2":1000000000}]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 4'000'000'000'000'000'000LL);
    EXPECT_EQ(body.at("perimeter").asInt(), 8'000'000'000LL);
}

TEST(App, UnicodeAndEscapesInId) {
    // \uXXXX escapes keep the payload plain ASCII; id decodes to 区块.
    auto [status, body] = call(
        "{\"rectangles\":[{\"x1\":0,\"y1\":0,\"x2\":1,\"y2\":1,"
        "\"id\":\"\\u533a\\u5757\"}]}");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("area").asInt(), 1);
}

TEST(App, ResponseHasSemantics) {
    auto [status, body] = call(R"({"rectangles":[{"x1":0,"y1":0,"x2":1,"y2":1}]})");
    ASSERT_EQ(status, 200);
    EXPECT_EQ(body.at("units").at("semantics").asString(),
              "half-open [x1,x2) x [y1,y2)");
}
