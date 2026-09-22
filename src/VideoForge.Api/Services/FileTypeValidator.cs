namespace VideoForge.Api.Services;

/// <summary>
/// Sniffs the real file type from magic bytes. The client-supplied file name
/// and content type are never trusted.
/// </summary>
public static class FileTypeValidator
{
    public sealed record SniffedType(string Kind, string Extension, string ContentType);

    private static readonly (byte[] Magic, int Offset, SniffedType Type)[] Signatures =
    {
        (new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }, 0, new("image", ".png", "image/png")),
        (new byte[] { 0xFF, 0xD8, 0xFF }, 0, new("image", ".jpg", "image/jpeg")),
        (new byte[] { 0x47, 0x49, 0x46, 0x38 }, 0, new("image", ".gif", "image/gif")),
        (new byte[] { 0x66, 0x74, 0x79, 0x70 }, 4, new("video", ".mp4", "video/mp4")),   // "ftyp" at offset 4 (mp4/mov/m4v)
        (new byte[] { 0x1A, 0x45, 0xDF, 0xA3 }, 0, new("video", ".webm", "video/webm")), // EBML (webm/mkv)
    };

    public const int SniffBytes = 12;

    public static bool TrySniff(ReadOnlySpan<byte> header, out SniffedType? type)
    {
        foreach (var (magic, offset, candidate) in Signatures)
        {
            if (header.Length >= offset + magic.Length && header.Slice(offset, magic.Length).SequenceEqual(magic))
            {
                type = candidate;
                return true;
            }
        }
        type = null;
        return false;
    }
}

/// <summary>
/// Resolves storage-relative paths and rejects anything escaping the root
/// (path traversal protection).
/// </summary>
public static class PathSafety
{
    public static string SafeCombine(string root, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative))
            throw new ArgumentException("relative path is empty", nameof(relative));
        if (Path.IsPathRooted(relative))
            throw new ArgumentException("absolute paths are not allowed", nameof(relative));

        var rootFull = Path.GetFullPath(root);
        var full = Path.GetFullPath(Path.Combine(rootFull, relative));
        if (!full.StartsWith(rootFull + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            throw new ArgumentException($"path escapes storage root: {relative}", nameof(relative));
        return full;
    }

    public static bool IsSafe(string root, string relative)
    {
        try { SafeCombine(root, relative); return true; }
        catch (ArgumentException) { return false; }
    }
}
