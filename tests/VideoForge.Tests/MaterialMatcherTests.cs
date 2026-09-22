using VideoForge.Api.Matching;
using VideoForge.Api.Models;
using Xunit;

namespace VideoForge.Tests;

public sealed class MaterialMatcherTests
{
    private static Material Mat(Guid id, string[] tags, DateTime? createdAt = null) =>
        new()
        {
            Id = id,
            Filename = id + ".png",
            Tags = tags,
            MediaKind = "image",
            CreatedAt = createdAt ?? new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc)
        };

    private static Scene Scene(params string[] keywords) =>
        new() { Position = 0, Description = "d", Keywords = keywords };

    [Fact]
    public void Score_is_number_of_keyword_tag_hits()
    {
        var catalog = new[]
        {
            Mat(Guid.NewGuid(), ["ocean", "wave"]),
            Mat(Guid.NewGuid(), ["ocean", "wave", "sunset"])
        };
        var selection = new MaterialMatcher().Match([Scene("ocean", "sunset", "wave")], catalog, 5000)[0];

        Assert.True(selection.Matched);
        Assert.Equal(3, selection.Score);
        Assert.Equal(catalog[1].Id, selection.Material!.Id);
    }

    [Fact]
    public void Matching_is_case_insensitive()
    {
        var catalog = new[] { Mat(Guid.NewGuid(), ["OCEAN", "SunSet"]) };
        var selection = new MaterialMatcher().Match([Scene("ocean", "sunset")], catalog, 5000)[0];
        Assert.Equal(2, selection.Score);
    }

    [Fact]
    public void Ties_break_by_created_at_then_id_deterministically()
    {
        var earlier = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        var later = earlier.AddHours(1);
        var a = Mat(Guid.Parse("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), ["ocean"], earlier);
        var b = Mat(Guid.Parse("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"), ["ocean"], later);

        var s1 = new MaterialMatcher().Match([Scene("ocean")], new[] { b, a }, 5000)[0];
        var s2 = new MaterialMatcher().Match([Scene("ocean")], new[] { a, b }, 5000)[0];

        Assert.Equal(a.Id, s1.Material!.Id); // input order must not matter
        Assert.Equal(a.Id, s2.Material!.Id);
    }

    [Fact]
    public void Ties_with_same_timestamp_break_by_id()
    {
        var ts = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        var a = Mat(Guid.Parse("11111111-1111-1111-1111-111111111111"), ["ocean"], ts);
        var b = Mat(Guid.Parse("22222222-2222-2222-2222-222222222222"), ["ocean"], ts);

        var selection = new MaterialMatcher().Match([Scene("ocean")], new[] { b, a }, 5000)[0];
        Assert.Equal(a.Id, selection.Material!.Id);
    }

    [Fact]
    public void Missing_material_returns_specific_reason()
    {
        var selection = new MaterialMatcher().Match([Scene("desert")], [Mat(Guid.NewGuid(), ["ocean"])], 5000)[0];
        Assert.False(selection.Matched);
        Assert.NotNull(selection.Reason);
        Assert.Contains("desert", selection.Reason);
    }

    [Fact]
    public void Empty_keywords_have_their_own_reason()
    {
        var selection = new MaterialMatcher().Match([Scene()], [Mat(Guid.NewGuid(), ["ocean"])], 5000)[0];
        Assert.False(selection.Matched);
        Assert.Contains("no keywords", selection.Reason, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData(3, 10000, new long[] { 3334, 3333, 3333 })]
    [InlineData(2, 5001, new long[] { 2501, 2500 })]
    [InlineData(4, 5000, new long[] { 1250, 1250, 1250, 1250 })]
    public void Duration_allocation_sums_to_target(int scenes, int target, long[] expected)
    {
        var alloc = MaterialMatcher.AllocateDurations(scenes, target);
        Assert.Equal(expected, alloc);
        Assert.Equal(target, alloc.Sum());
    }
}
