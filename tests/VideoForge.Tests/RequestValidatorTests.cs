using VideoForge.Api.Contracts;
using VideoForge.Api.Validation;
using Xunit;

namespace VideoForge.Tests;

public sealed class RequestValidatorTests
{
    private static CreateProjectRequest Valid() => new(
        "demo", 10_000, "fade",
        [new SceneInput("A calm beach", ["ocean", "beach"])]);

    [Fact]
    public void Accepts_valid_project()
    {
        var (v, err) = RequestValidator.ValidateProject(Valid());
        Assert.Null(err);
        Assert.Equal("demo", v!.Name);
        Assert.Equal("fade", v.Transition);
    }

    [Theory]
    [InlineData(4999)]
    [InlineData(60001)]
    public void Rejects_duration_outside_5_to_60_seconds(int ms)
    {
        var req = Valid() with { TargetDurationMs = ms };
        Assert.NotNull(RequestValidator.ValidateProject(req).Error);
    }

    [Fact]
    public void Accepts_boundary_durations()
    {
        Assert.Null(RequestValidator.ValidateProject(Valid() with { TargetDurationMs = 5000 }).Error);
        Assert.Null(RequestValidator.ValidateProject(Valid() with { TargetDurationMs = 60000 }).Error);
    }

    [Fact]
    public void Rejects_more_than_20_scenes()
    {
        var scenes = Enumerable.Range(0, 21)
            .Select(i => new SceneInput($"scene {i}", ["ocean"])).ToList();
        Assert.NotNull(RequestValidator.ValidateProject(Valid() with { Scenes = scenes }).Error);
    }

    [Fact]
    public void Accepts_exactly_20_scenes()
    {
        var scenes = Enumerable.Range(0, 20)
            .Select(i => new SceneInput($"scene {i}", ["ocean"])).ToList();
        Assert.Null(RequestValidator.ValidateProject(Valid() with { Scenes = scenes }).Error);
    }

    [Fact]
    public void Rejects_description_over_500_chars()
    {
        var req = Valid() with
        {
            Scenes = [new SceneInput(new string('x', 501), ["ocean"])]
        };
        Assert.NotNull(RequestValidator.ValidateProject(req).Error);
    }

    [Fact]
    public void Rejects_scene_without_keywords()
    {
        var req = Valid() with { Scenes = [new SceneInput("x", [])] };
        Assert.NotNull(RequestValidator.ValidateProject(req).Error);
    }

    [Fact]
    public void Rejects_bad_transition()
    {
        Assert.NotNull(RequestValidator.ValidateProject(Valid() with { Transition = "explode" }).Error);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("bad key!")]
    [InlineData("../etc/passwd")]
    public void Rejects_bad_submission_keys(string? key)
    {
        Assert.NotNull(RequestValidator.ValidateSubmissionKey(key).Error);
    }

    [Fact]
    public void Accepts_safe_submission_key()
    {
        var (k, err) = RequestValidator.ValidateSubmissionKey("order-123_v2:ab");
        Assert.Null(err);
        Assert.Equal("order-123_v2:ab", k);
    }
}
