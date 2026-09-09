<#
.SYNOPSIS
    楽天 価格ウォッチャー（lesson-5-1 課題2）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    **登録には管理者が要る**（-VerifyOnly のときは要らない）。

    登録したことは、動いていることの証拠にならない。このスクリプトは登録のあと
    **読み戻して照合する**が、それでも確かめているのは「登録した内容が意図どおりか」
    までである。本当の検証は **翌日シートに行が増えたか**で行う。

    引き金を2つ置くのは、家庭用 PC だと 21 時に電源が入っているとは限らないため
    （DESIGN 5-W）。ログオン時の引き金に 5 分の遅延を入れるのは、起動直後だと
    ネットワークが上がりきっておらず、全件が「取れなかった」として記録され、
    **実在しない欠測の日**ができるため（DESIGN 5-AB）。

    2つの引き金があると同じ日に 2 回走りうるが、`run_daily.py` 側が
    「その日に取得できた行があるか」を見て何もせずに終わる（DESIGN 5-X）。

.PARAMETER At
    毎日の実行時刻。既定は 21:00。

.PARAMETER Force
    すでに同じ名前のタスクがあるとき、消してから作り直す。

.PARAMETER VerifyOnly
    登録せず、**いまの登録内容を照合するだけ**。読むだけなので昇格が要らない。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File task2\tools\register_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File task2\tools\register_task.ps1 -VerifyOnly
#>
param(
    [string]$At = "21:00",
    [switch]$Force,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

$Name = "RakutenPriceWatcher"
# task2\tools → task2 → lesson-5-1
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Script = Join-Path $Root "task2\run_daily.py"

# ---------------------------------------------------------------- 照合の道具

function Test-Setting {
    <#
        設定を1つ照合する。**名前が無いこと自体を食い違いとして返す。**

        書くときの名前と、読むときの名前は同じではない。
        `New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries` で設定したものは、
        読み戻すと `DisallowStartIfOnBatteries`（否定形）として入っている。

        書く側の名前で読むと存在しないプロパティになり、**PowerShell は $null を
        返して黙る**。それを「設定されていない」と読むと、正しく入っている設定を
        食い違いとして報告する——2026-09-09 に実際にそう報告した。

        だから **$null と False を区別する**。名前が無いなら、
        「設定が違う」ではなく「読めていない」と言う。
    #>
    param($Settings, [string]$Name, $Expected)

    if ($Settings.PSObject.Properties.Name -notcontains $Name) {
        return "設定 $Name を読めない（プロパティ名が違う可能性。照合が空振りしている）"
    }
    if ($Settings.$Name -ne $Expected) {
        return "$Name が $($Settings.$Name)（期待 $Expected）"
    }
    return $null
}

# ---------------------------------------------------------------- 先に確かめる

if (-not $VerifyOnly) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "管理者として実行してください。タスクの登録には昇格が要ります。" -ForegroundColor Red
        Write-Host "  PowerShell を右クリック →「管理者として実行」→ もう一度このスクリプトを走らせる"
        Write-Host "  （登録済みの内容を見るだけなら -VerifyOnly を付けてください）"
        exit 1
    }

    # **走らせる先が実在することを、登録の前に確かめる。**
    # 存在しないパスでも登録は成功する。失敗するのは初回の実行時で、
    # そのとき標準出力は捨てられているので理由が見えない（DESIGN 5-V / 5-Z）。
    foreach ($path in @($Python, $Script)) {
        if (-not (Test-Path -LiteralPath $path)) {
            Write-Host "見つかりません: $path" -ForegroundColor Red
            exit 1
        }
    }

    $existing = Get-ScheduledTask | Where-Object { $_.TaskName -eq $Name }
    if ($existing) {
        if (-not $Force) {
            Write-Host "すでに登録されています: $Name" -ForegroundColor Yellow
            Write-Host "  作り直すなら -Force を付けてください（いまの設定は消えます）"
            Write-Host "  いまの内容を照合するだけなら -VerifyOnly"
            exit 1
        }
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "既存のタスクを消しました: $Name"
    }

    # ------------------------------------------------------------ 登録する

    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument ('"' + $Script + '"') `
        -WorkingDirectory $Root

    $daily = New-ScheduledTaskTrigger -Daily -At $At
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $logon.Delay = "PT5M"

    # StartWhenAvailable は「時刻を過ぎていたら、できるだけ早く始める」。
    # 電源が入っていなかった日を、次に起動したときに拾うために要る（DESIGN 5-W）。
    # MultipleInstances=IgnoreNew はロック（DESIGN 5-AC）と二重の守り。
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    $who = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    $description = @(
        "楽天の商品価格を取得してスプレッドシートへ追記する（lesson-5-1 課題2）。"
        "記録は task2\logs\run.jsonl。失敗と値下がりは LINE へ、週に1度は生存通知が飛ぶ。"
        "検証は登録の成否ではなく、翌日シートに行が増えたかで行う。"
    ) -join " "

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $daily, $logon `
        -Settings $settings `
        -Principal $who `
        -Description $description | Out-Null
}

# ---------------------------------------------------------------- 読み戻して照合する
#
# **この照合そのものをわざと壊して確かめた**（2026-09-09）:
#   kill    期待値を裏返す（DisallowStartIfOnBatteries に True を期待させる）
#   kill    書く側の名前で読む（AllowStartIfOnBatteries は存在しない＝今回のバグ）
#   素通り  引き金の数を見る行を消す
#
# 素通りしたのは、**いまのタスクが正しい状態だから**——検査を消しても差が出ない。
# 守るには「引き金が1つのタスク」を作って照合する必要があり、それには昇格が要る。
# **ここは Python 側のテストほどの網を張っていない。** 分かったうえで置いている。

$task = Get-ScheduledTask | Where-Object { $_.TaskName -eq $Name }
if (-not $task) {
    Write-Host "登録されていません: $Name" -ForegroundColor Red
    exit 1
}
$task = Get-ScheduledTask -TaskName $Name

$problems = @()

if ($task.Actions.Count -ne 1) { $problems += "動作の数が 1 ではない: $($task.Actions.Count)" }
else {
    if ($task.Actions[0].Execute -ne $Python) { $problems += "実行するものが違う: $($task.Actions[0].Execute)" }
    if ($task.Actions[0].Arguments -notlike "*run_daily.py*") { $problems += "引数が違う: $($task.Actions[0].Arguments)" }
    if ($task.Actions[0].WorkingDirectory -ne $Root) { $problems += "作業ディレクトリが違う: $($task.Actions[0].WorkingDirectory)" }
}

if ($task.Triggers.Count -ne 2) { $problems += "引き金の数が 2 ではない: $($task.Triggers.Count)" }
$daily_back = $task.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskDailyTrigger" }
$logon_back = $task.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" }
if (-not $daily_back) { $problems += "毎日の引き金が無い" }
elseif ($daily_back.StartBoundary -notlike "*T$($At):00*") { $problems += "時刻が違う: $($daily_back.StartBoundary)" }
if (-not $logon_back) { $problems += "ログオン時の引き金が無い" }
elseif ($logon_back.Delay -ne "PT5M") { $problems += "ログオン時の遅延が 5 分ではない: $($logon_back.Delay)" }

# **保存されている名前で読む。** 書くときの名前（-AllowStartIfOnBatteries）は
# ここには無い。無い名前で読むと $null が返り、照合はそれを「違う」と言う。
foreach ($expectation in @(
    @{ Name = "StartWhenAvailable";         Expected = $true },
    @{ Name = "DisallowStartIfOnBatteries"; Expected = $false },
    @{ Name = "StopIfGoingOnBatteries";     Expected = $false },
    @{ Name = "MultipleInstances";          Expected = "IgnoreNew" }
)) {
    $problem = Test-Setting -Settings $task.Settings -Name $expectation.Name -Expected $expectation.Expected
    if ($problem) { $problems += $problem }
}

Write-Host ""
Write-Host "$(if ($VerifyOnly) { '照合しました' } else { '登録しました' }): $($task.URI)"
Write-Host "  実行          $($task.Actions[0].Execute)"
Write-Host "  引数          $($task.Actions[0].Arguments)"
Write-Host "  作業場所      $($task.Actions[0].WorkingDirectory)"
Write-Host "  引き金        毎日 $At ／ ログオンの 5 分後"
Write-Host "  電源          バッテリー駆動でも開始する・切り替わっても止めない"
Write-Host "  状態          $($task.State)"
Write-Host "  照合          $($problems.Count) 件の食い違い"

if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "食い違い:" -ForegroundColor Red
    foreach ($problem in $problems) { Write-Host "  - $problem" -ForegroundColor Red }
    exit 1
}

Write-Host ""
Write-Host "**登録できたことは、動いている証拠にならない。**" -ForegroundColor Yellow
Write-Host "明日 21 時のあとに、次の2つで確かめること:"
Write-Host "  1. シートに行が増えているか（これが本当の検証）"
Write-Host "  2. task2\logs\run.jsonl の最後の行"
exit 0
