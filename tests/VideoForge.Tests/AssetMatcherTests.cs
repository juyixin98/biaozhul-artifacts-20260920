using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Tests;

public class AssetMatcherTests
{
    private static Asset Asset(long id, params string[] tags) =>
        new() { Id = id, Name = $"a{id}", Kind = "image", Tags = tags };

    private static Project Project(string transition, params string[] descriptions) =>
        new()
        {
            Id = 1,
            Name = "p",
            TargetDurationSeconds = 10,
            Transition = transition,
            Scenes = descriptions.Select((d, i) => new Scene { Ord = i + 1, Description = d }).ToList(),
        };

    private readonly AssetMatcher _matcher = new();

    [Fact]
    public void Picks_highest_scoring_asset()
    {
        var assets = new[]
        {
            Asset(1, "city", "night"),
            Asset(2, "city", "night", "neon"),
        };
        var plan = _matcher.BuildPlan(Project("cut", "neon lights over the city at night"), assets);
        Assert.Single(plan.Segments);
        Assert.Equal(2, plan.Segments[0].Asset.Id); // 3 tags matched beats 2
    }

    [Fact]
    public void Ties_break_by_fixed_asset_id_order()
    {
        var assets = new[]
        {
            Asset(7, "beach"),
            Asset(3, "beach"),
            Asset(9, "beach"),
        };
        var plan = _matcher.BuildPlan(Project("cut", "a quiet beach"), assets);
        Assert.Equal(3, plan.Segments[0].Asset.Id); // lowest id wins, deterministically
    }

    [Fact]
    public void Matching_is_case_insensitive()
    {
        var plan = _matcher.BuildPlan(Project("cut", "SUNSET over the Ocean"), new[] { Asset(1, "sunset") });
        Assert.Equal(1, plan.Segments[0].Asset.Id);
    }

    [Fact]
    public void Missing_asset_reports_scene_and_keywords()
    {
        var ex = Assert.Throws<NoAssetMatchException>(() =>
            _matcher.BuildPlan(Project("cut", "a volcano erupting", "calm forest"), new[] { Asset(1, "forest") }));
        Assert.Contains("scene 1", ex.Message);
        Assert.Contains("volcano", ex.Message);
        Assert.Contains("forest", ex.Message); // available tags listed
    }

    [Fact]
    public void Segment_durations_sum_to_target_for_cut()
    {
        var plan = _matcher.BuildPlan(
            Project("cut", "red square", "blue circle", "green field"),
            new[] { Asset(1, "red"), Asset(2, "blue"), Asset(3, "green") });
        Assert.Equal(3, plan.Segments.Count);
        Assert.Equal(10.0, plan.Segments.Sum(s => s.DurationSeconds), 3);
    }

    [Fact]
    public void Fade_transition_compensates_for_xfade_overlap()
    {
        var plan = _matcher.BuildPlan(
            Project("fade", "red square", "blue circle", "green field"),
            new[] { Asset(1, "red"), Asset(2, "blue"), Asset(3, "green") });
        // total = sum(durs) - (n-1)*fade must equal target
        var total = plan.Segments.Sum(s => s.DurationSeconds) - 2 * AssetMatcher.FadeDurationSeconds;
        Assert.Equal(10.0, total, 3);
    }
}
