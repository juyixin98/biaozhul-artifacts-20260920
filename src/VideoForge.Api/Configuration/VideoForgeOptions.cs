namespace VideoForge.Api.Configuration;

public sealed class VideoForgeOptions
{
    public const string SectionName = "VideoForge";

    /// <summary>Root directory for stored assets and published/temp outputs.</summary>
    public string DataDirectory { get; set; } = "/var/lib/videoforge";

    /// <summary>Max upload size per material, bytes. Requirement: 50 MB.</summary>
    public long MaxMaterialBytes { get; set; } = 50 * 1024 * 1024;

    /// <summary>Max simultaneous render jobs. Requirement: 3.</summary>
    public int MaxParallelJobs { get; set; } = 3;

    /// <summary>Poll interval of the queue worker.</summary>
    public int PollIntervalMs { get; set; } = 300;

    /// <summary>Heartbeat staleness after which a processing job is reclaimed.</summary>
    public int StaleJobSeconds { get; set; } = 45;

    /// <summary>Max automatic attempts per job (first run + retries).</summary>
    public int MaxAttempts { get; set; } = 3;

    /// <summary>Output video width.</summary>
    public int OutputWidth { get; set; } = 1280;

    /// <summary>Output video height.</summary>
    public int OutputHeight { get; set; } = 720;

    /// <summary>ffmpeg / ffprobe binaries.</summary>
    public string FFmpegPath { get; set; } = "ffmpeg";
    public string FFprobePath { get; set; } = "ffprobe";

    public string AssetsDir => Path.Combine(DataDirectory, "assets");
    public string TempDir => Path.Combine(DataDirectory, "tmp");
    public string PublishedDir => Path.Combine(DataDirectory, "published");
}
