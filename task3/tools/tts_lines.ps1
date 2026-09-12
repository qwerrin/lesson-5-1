# ASCII ONLY -- do not put Japanese (or any non-ASCII) in this file.
#
# Windows PowerShell 5.1 reads a BOM-less .ps1 as cp932. A UTF-8 Japanese
# comment can end on a cp932 lead byte, which swallows the following newline
# and turns an unrelated later line into a parse error. The Japanese script
# lives in -LinesPath (UTF-8) and is read with -Encoding UTF8 instead.
#
# Why WinRT and not System.Speech: System.Speech only sees the SAPI5 registry
# hive, which on this machine holds exactly one ja-JP voice (Haruka Desktop).
# The other Japanese voices live under Speech_OneCore and are reachable only
# through Windows.Media.SpeechSynthesis. Three speakers need that path.
#
# This script decides nothing. It reads "voice|gap|text" lines and writes one
# wav per line. Parsing, timing and mixing belong to build_audio.py.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$LinesPath,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = "Stop"

# AsTask() lives in System.Runtime.WindowsRuntime, which is not loaded just
# because a WinRT type was projected.
Add-Type -AssemblyName System.Runtime.WindowsRuntime

# PowerShell 5.1 has no await; drive the WinRT IAsyncOperation synchronously.
function Await($op, $type) {
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}

[Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataReader, Windows.Storage, ContentType = WindowsRuntime] | Out-Null

$allVoices = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
$syn = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer

# @() matters: Get-Content returns a bare string for a one-line file, and
# indexing a string yields a Char.
$lines = @(Get-Content -LiteralPath $LinesPath -Encoding UTF8)
if ($lines.Count -eq 0) { throw "no lines in $LinesPath" }

# Resolve every voice before speaking anything: a typo on line 40 should not
# surface after 39 syntheses.
$wanted = @{}
foreach ($line in $lines) {
    $bar = $line.IndexOf('|')
    if ($bar -lt 1) { throw "no separator in line: $line" }
    $wanted[$line.Substring(0, $bar)] = $true
}
$voiceOf = @{}
foreach ($name in $wanted.Keys) {
    $v = $allVoices | Where-Object { $_.DisplayName -eq "Microsoft $name" } | Select-Object -First 1
    if ($null -eq $v) {
        $have = ($allVoices | ForEach-Object { $_.DisplayName }) -join ', '
        throw "voice not available: Microsoft $name (have: $have)"
    }
    $voiceOf[$name] = $v
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$index = 0
foreach ($line in $lines) {
    $bar = $line.IndexOf('|')
    $voiceName = $line.Substring(0, $bar)
    $rest = $line.Substring($bar + 1)
    $bar2 = $rest.IndexOf('|')
    if ($bar2 -lt 0) { throw "no second separator in line: $line" }
    $text = $rest.Substring($bar2 + 1)
    if ($text.Trim().Length -eq 0) { throw "empty text on line $index" }

    $syn.Voice = $voiceOf[$voiceName]
    $stream = Await $syn.SynthesizeTextToStreamAsync($text) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])

    $size = [uint32]$stream.Size
    $dr = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
    Await $dr.LoadAsync($size) ([uint32]) | Out-Null
    $bytes = New-Object byte[] $size
    $dr.ReadBytes($bytes)
    $dr.Dispose()
    $stream.Dispose()

    # Read the RIFF header back rather than trusting the call succeeded: a wav
    # with a header and no samples opens fine and plays as silence.
    if ([Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne 'RIFF') {
        throw "line ${index}: not a RIFF stream"
    }
    $byteRate = [BitConverter]::ToInt32($bytes, 28)
    $seconds = ($bytes.Length - 44) / $byteRate
    if ($seconds -le 0) { throw "line ${index}: empty audio" }

    $out = Join-Path $OutDir ("{0:d3}.wav" -f $index)
    [System.IO.File]::WriteAllBytes($out, $bytes)
    "{0:d3} {1,-8} {2,8} bytes {3,7:f3} sec" -f $index, $voiceName, $bytes.Length, $seconds
    $index++
}

"lines: $index"
