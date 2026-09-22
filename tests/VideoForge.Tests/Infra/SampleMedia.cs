using System.Diagnostics;
using Xunit;

namespace VideoForge.Tests.Infra;

/// <summary>
/// Generates real sample media with the host ffmpeg (same mechanism the
/// bundled scripts use), so tests exercise actual decoding/rendering.
/// </summary>
public static class SampleMedia
{
    public static string EnsurePng(string dir, string name, string color)
    {
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, name);
        if (!File.Exists(path))
            RunFFmpeg($"-y -f lavfi -i color=c={color}:s=640x480:d=1 -frames:v 1 \"{path}\"");
        return path;
    }

    public static string EnsureMp4(string dir, string name, string color, double seconds = 2)
    {
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, name);
        if (!File.Exists(path))
            RunFFmpeg(
                $"-y -f lavfi -i color=c={color}:s=640x480:r=30:d={seconds.ToString(System.Globalization.CultureInfo.InvariantCulture)} " +
                $"-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 -shortest -c:v libx264 -pix_fmt yuv420p \"{path}\"");
        return path;
    }

    public static byte[] MinimalPng() =>
        Convert.FromBase64String(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC");

    private static void RunFFmpeg(string args)
    {
        var psi = new ProcessStartInfo("ffmpeg")
        {
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            UseShellExecute = false
        };
        foreach (var a in args.Split(' ', StringSplitOptions.RemoveEmptyEntries))
            psi.ArgumentList.Add(a.Trim('"'));
        using var proc = Process.Start(psi)!;
        var err = proc.StandardError.ReadToEnd();
        proc.WaitForExit(30_000);
        Assert.True(proc.HasExited && proc.ExitCode == 0, $"ffmpeg failed: {err}");
    }
}
