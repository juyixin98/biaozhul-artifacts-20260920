using VideoForge.Api.Services;

namespace VideoForge.Tests;

public class FileTypeValidatorTests
{
    [Theory]
    [InlineData(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }, "image", ".png")]
    [InlineData(new byte[] { 0xFF, 0xD8, 0xFF, 0xE0, 0, 0, 0, 0 }, "image", ".jpg")]
    [InlineData(new byte[] { 0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0, 0 }, "image", ".gif")]
    [InlineData(new byte[] { 0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6F, 0x6D }, "video", ".mp4")]
    [InlineData(new byte[] { 0x1A, 0x45, 0xDF, 0xA3, 0, 0, 0, 0 }, "video", ".webm")]
    public void Accepts_known_signatures(byte[] header, string kind, string ext)
    {
        Assert.True(FileTypeValidator.TrySniff(header, out var type));
        Assert.Equal(kind, type!.Kind);
        Assert.Equal(ext, type.Extension);
    }

    [Theory]
    [InlineData(new byte[] { 0x4D, 0x5A, 0x90, 0, 0, 0, 0, 0 })] // PE executable
    [InlineData(new byte[] { 0x7F, 0x45, 0x4C, 0x46, 0, 0, 0, 0 })] // ELF
    [InlineData(new byte[] { 0x50, 0x4B, 0x03, 0x04, 0, 0, 0, 0 })] // zip (e.g. renamed docx)
    [InlineData(new byte[] { 0x25, 0x50, 0x44, 0x46, 0, 0, 0, 0 })] // pdf
    public void Rejects_unknown_or_dangerous_content(byte[] header)
    {
        Assert.False(FileTypeValidator.TrySniff(header, out _));
    }

    [Fact]
    public void Extension_alone_is_not_enough()
    {
        // A text file renamed to .png must be rejected by content sniffing.
        var textBytes = "this is definitely not a png"u8.ToArray();
        Assert.False(FileTypeValidator.TrySniff(textBytes, out _));
    }
}

public class PathSafetyTests
{
    private static readonly string Root = Path.Combine(Path.GetTempPath(), "videoforge-rootsafety");

    [Theory]
    [InlineData("../escape.mp4")]
    [InlineData("assets/../../escape.mp4")]
    [InlineData("..")]
    [InlineData("assets/../../../etc/passwd")]
    public void Rejects_traversal(string relative)
    {
        Assert.False(PathSafety.IsSafe(Root, relative));
        Assert.Throws<ArgumentException>(() => PathSafety.SafeCombine(Root, relative));
    }

    [Theory]
    [InlineData("/etc/passwd")]
    public void Rejects_absolute_paths(string relative)
    {
        Assert.False(PathSafety.IsSafe(Root, relative));
    }

    [Theory]
    [InlineData("assets/abc.mp4")]
    [InlineData("outputs/42.mp4")]
    public void Accepts_normal_relative_paths(string relative)
    {
        var full = PathSafety.SafeCombine(Root, relative);
        Assert.StartsWith(Path.GetFullPath(Root), full);
    }
}
