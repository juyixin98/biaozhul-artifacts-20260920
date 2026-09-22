using System.Security.Cryptography;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Api.Controllers;

[ApiController]
[Route("api/assets")]
public sealed class AssetsController : ControllerBase
{
    private readonly AssetRepository _assets;
    private readonly IVideoRenderer _renderer;
    private readonly VideoForgeOptions _options;

    public AssetsController(AssetRepository assets, IVideoRenderer renderer, IOptions<VideoForgeOptions> options)
    {
        _assets = assets;
        _renderer = renderer;
        _options = options.Value;
    }

    [HttpGet]
    public async Task<IActionResult> List(CancellationToken ct) =>
        Ok((await _assets.ListAsync(ct)).Select(a => a.ToDto()));

    /// <summary>Import a local asset. The real file type is sniffed from magic bytes.</summary>
    [HttpPost]
    [RequestSizeLimit(60L * 1024 * 1024)]
    public async Task<IActionResult> Import(
        IFormFile file,
        [FromForm] string name,
        [FromForm] string? tags,
        CancellationToken ct)
    {
        if (file is null || file.Length == 0)
            return BadRequest(new { error = "file is required" });
        if (file.Length > _options.MaxAssetBytes)
            return UnprocessableEntity(new { error = $"file exceeds the {_options.MaxAssetBytes / (1024 * 1024)} MB limit" });
        if (string.IsNullOrWhiteSpace(name) || name.Length > 200)
            return BadRequest(new { error = "name is required (1-200 chars)" });

        var tagList = (tags ?? "")
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Where(t => t.Length <= 50).Take(20).ToArray();
        if (tagList.Length == 0)
            return BadRequest(new { error = "at least one tag is required" });

        // Stream to disk with a hard size cap, hashing on the way; sniff the header.
        Directory.CreateDirectory(_options.AssetsDir);
        var tmpPath = Path.Combine(_options.TmpDir, $"upload-{Guid.NewGuid():N}.bin");
        Directory.CreateDirectory(_options.TmpDir);
        try
        {
            string sha256;
            var header = new byte[FileTypeValidator.SniffBytes];
            var headerLen = 0;
            long written = 0;
            await using (var input = file.OpenReadStream())
            await using (var output = System.IO.File.Create(tmpPath))
            {
                using var hasher = SHA256.Create();
                var buffer = new byte[64 * 1024];
                int read;
                while ((read = await input.ReadAsync(buffer, ct)) > 0)
                {
                    written += read;
                    if (written > _options.MaxAssetBytes)
                        return UnprocessableEntity(new { error = $"file exceeds the {_options.MaxAssetBytes / (1024 * 1024)} MB limit" });
                    if (headerLen < header.Length)
                    {
                        var copy = Math.Min(read, header.Length - headerLen);
                        Array.Copy(buffer, 0, header, headerLen, copy);
                        headerLen += copy;
                    }
                    hasher.TransformBlock(buffer, 0, read, null, 0);
                    await output.WriteAsync(buffer.AsMemory(0, read), ct);
                }
                hasher.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                sha256 = Convert.ToHexString(hasher.Hash!).ToLowerInvariant();
            }

            if (!FileTypeValidator.TrySniff(header, out var sniffed) || sniffed is null)
                return UnprocessableEntity(new
                {
                    error = "unsupported or corrupt file: content does not match a known image/video signature " +
                            "(png, jpeg, gif, mp4/mov, webm)"
                });

            var relPath = Path.Combine("assets", $"{Guid.NewGuid():N}{sniffed.Extension}");
            var finalPath = PathSafety.SafeCombine(_options.StorageRoot, relPath);
            System.IO.File.Move(tmpPath, finalPath);

            double? duration = null;
            if (sniffed.Kind == AssetKinds.Video)
                duration = await _renderer.ProbeDurationSecondsAsync(finalPath, ct);

            var asset = await _assets.InsertAsync(new Asset
            {
                Name = name.Trim(),
                Kind = sniffed.Kind,
                Tags = tagList,
                StoredPath = relPath,
                OriginalFileName = Path.GetFileName(file.FileName ?? "upload"),
                ContentType = sniffed.ContentType,
                SizeBytes = written,
                Sha256 = sha256,
                DurationSeconds = duration,
            }, ct);
            return Created($"/api/assets/{asset.Id}", asset.ToDto());
        }
        finally
        {
            if (System.IO.File.Exists(tmpPath))
                System.IO.File.Delete(tmpPath);
        }
    }
}
