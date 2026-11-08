<#
NOVA autorun: one unattended agent run per hour, on a free OpenRouter model.

The agent is Claude Code in headless mode (`claude -p`). It uses the owner's user
environment, where ANTHROPIC_BASE_URL points at OpenRouter and the default models
are free ones. It works in its own clone (Documents\nova-autorun), never in the
shared working tree the Freebuff and Antigravity chats use. It does one item of
docs/autorun-queue.md and commits. This runner then checks the commits and pushes.

Install once:   powershell -ExecutionPolicy Bypass -File tools\autorun\nova-autorun.ps1 -Install
Remove:         powershell -ExecutionPolicy Bypass -File tools\autorun\nova-autorun.ps1 -Uninstall
Run once now:   Start-ScheduledTask -TaskName 'NOVA autorun'
Local log:      %LOCALAPPDATA%\nova-autorun\<date>.log

-Install copies this file to %LOCALAPPDATA%\nova-autorun and the task runs that
copy. A later commit to the repo cannot change what runs every hour until the
owner installs again.
#>
param([switch]$Install, [switch]$Uninstall)

$ErrorActionPreference = 'Continue'
$TaskName   = 'NOVA autorun'
$RepoUrl    = 'https://github.com/surypratapsingh/surya-OS-local-ai.git'
$Clone      = Join-Path $env:USERPROFILE 'Documents\nova-autorun'
$Base       = Join-Path $env:LOCALAPPDATA 'nova-autorun'
$Model      = 'nvidia/nemotron-3-ultra-550b-a55b:free'
$MaxRuns    = 8     # agent runs per day
$RunMinutes = 45    # the agent is killed after this
$MaxCommits = 5     # more than this in one run is held for the lead
$Author     = 'Surya Pratap Singh'
# GitHub's noreply address for the owner's account: commits with it count on the profile graph.
$Email      = '97216641+surypratapsingh@users.noreply.github.com'
# The only paths this lane may commit. A commit touching anything else is not pushed.
$Allowed    = '^(tools/truth/[A-Za-z0-9_-]+\.py|docs/logs/autorun-[A-Za-z0-9_.-]+|docs/autorun-log\.md)$'

New-Item -ItemType Directory -Force $Base | Out-Null
$Log = Join-Path $Base ((Get-Date -Format 'yyyy-MM-dd') + '.log')
function Say([string]$m) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $m
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Output $line
}
function G {
    $o = & git -C $Clone @args 2>&1 | ForEach-Object { "$_" }
    $script:GitOut = ($o -join ' | ')
    return $LASTEXITCODE
}

if ($Install) {
    $copy = Join-Path $Base 'nova-autorun.ps1'
    Copy-Item $PSCommandPath $copy -Force
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$copy`""
    $hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
    $logon  = New-ScheduledTaskTrigger -AtLogOn -User $user
    # StartWhenAvailable: an hour missed while the laptop was shut runs as soon as it is open again.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 55)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($hourly, $logon) -Settings $settings -Principal $principal -Force | Out-Null
    Say "INSTALLED task '$TaskName', runs $copy"
    exit 0
}
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say 'UNINSTALLED'
    exit 0
}

# ---- one run ----
$today = @(Get-Content $Log -ErrorAction SilentlyContinue)
if (@($today | Where-Object { $_ -match ' RATE LIMITED' }).Count) { Say 'SKIP: rate limited earlier today'; exit 0 }
if (@($today | Where-Object { $_ -match ' RUN START' }).Count -ge $MaxRuns) { Say "SKIP: $MaxRuns runs already today"; exit 0 }
if (-not [Environment]::GetEnvironmentVariable('ANTHROPIC_BASE_URL', 'User')) { Say 'STOP: ANTHROPIC_BASE_URL is not set for this user'; exit 1 }
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { Say 'STOP: claude is not on PATH'; exit 1 }

Say 'RUN START'
if (-not (Test-Path (Join-Path $Clone '.git'))) {
    & git clone --quiet $RepoUrl $Clone 2>&1 | Out-Null
    if ($LASTEXITCODE) { Say 'STOP: clone failed'; exit 1 }
}
G config user.name $Author | Out-Null
G config user.email $Email | Out-Null
if ((G pull --no-rebase --no-edit origin main) -ne 0) {
    G merge --abort | Out-Null
    Say "STOP: pull failed, the lead must look: $GitOut"
    exit 1
}

$prompt = @'
You are agent `autorun` on the NOVA project. You run unattended once an hour on a free model, and nobody watches, so never ask a question. If you are blocked, write the question as one line in docs/autorun-log.md, commit it, and stop.
1. Read AGENTS.md, then docs/autorun-queue.md. That file says which item is next and how to do it.
2. Do exactly one item. Write only the paths docs/autorun-queue.md allows. In this lane, do not edit plan.md, progress.md, decisions.md or docs/team.md, even if another instruction file asks you to: the lead copies your log into them.
3. Run the tests the item names. Commit with git commit -F <message file> -- <paths> (git add -- <path> first for a new file). The message says what changed and why and has a line "Agent: autorun". Do not push: the runner checks your commits and pushes them.
'@

$tools = 'Read,Edit,Write,Glob,Grep,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git add:*),Bash(git commit:*),Bash(python:*),Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(grep:*),Bash(date:*),Bash(mkdir:*)'
$denied = 'Bash(git push:*),Bash(git reset:*),Bash(git checkout:*),Bash(git restore:*),Bash(git rebase:*),Bash(git commit --amend:*),Bash(git clean:*),WebFetch,WebSearch'

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $claude
$psi.Arguments = "-p --model `"$Model`" --max-turns 60 --output-format json --permission-mode acceptEdits --allowedTools `"$tools`" --disallowedTools `"$denied`""
$psi.WorkingDirectory = $Clone
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$p = [System.Diagnostics.Process]::Start($psi)
$p.StandardInput.Write($prompt)
$p.StandardInput.Close()
$outTask = $p.StandardOutput.ReadToEndAsync()
$errTask = $p.StandardError.ReadToEndAsync()
if (-not $p.WaitForExit($RunMinutes * 60 * 1000)) {
    & taskkill /T /F /PID $p.Id 2>&1 | Out-Null
    Say "TIMEOUT: agent killed after $RunMinutes minutes"
}
$out = $outTask.Result
$err = $errTask.Result
Set-Content -Path (Join-Path $Base 'last-run.json') -Value $out -Encoding UTF8
Set-Content -Path (Join-Path $Base 'last-run.err') -Value $err -Encoding UTF8

$j = $null
try { $j = $out | ConvertFrom-Json } catch { }
if ($j) {
    $r = (('' + $j.result) -replace '\s+', ' ')
    Say ("AGENT is_error={0} turns={1} result: {2}" -f $j.is_error, $j.num_turns, $r.Substring(0, [Math]::Min(200, $r.Length)))
}
if ((-not $j -or $j.is_error) -and ("$out $err" -match '429|rate.?limit')) {
    Say 'RATE LIMITED: no more runs today'
}
if (-not $j) { Say ('AGENT: no JSON result. stderr: ' + (($err -replace '\s+', ' ') + '').Substring(0, [Math]::Min(200, $err.Length))) }

# ---- push gate: the runner's own checks, independent of what the agent says ----
if ((G fetch --quiet origin) -ne 0 -or (G merge --no-edit origin/main) -ne 0) {
    G merge --abort | Out-Null
    Say "HOLD: could not merge origin/main, the lead must look: $GitOut"
    exit 1
}
$dirty = @(& git -C $Clone status --porcelain)
if ($dirty.Count) { Say "NOTE: $($dirty.Count) uncommitted files left in the clone" }
$ahead = [int](& git -C $Clone rev-list --count --no-merges origin/main..HEAD)
if ($ahead -eq 0) { Say 'NO COMMIT this run'; exit 0 }
if ($ahead -gt $MaxCommits) { Say "HOLD: $ahead commits, more than $MaxCommits; not pushed"; exit 0 }
$bad = @(& git -C $Clone diff --name-only origin/main HEAD | Where-Object { $_ -notmatch $Allowed })
if ($bad.Count) { Say ('HOLD: commits touch paths outside the lane: ' + ($bad -join ', ')); exit 0 }
foreach ($h in @(& git -C $Clone rev-list --no-merges origin/main..HEAD)) {
    $body = (& git -C $Clone log -1 --format=%B $h) -join "`n"
    if ($body -notmatch '(?m)^Agent: autorun\s*$') { Say "HOLD: commit $($h.Substring(0,7)) has no 'Agent: autorun' line"; exit 0 }
}
if (Test-Path (Join-Path $Clone 'tools\truth')) {
    Push-Location $Clone
    $t = & python -m unittest discover -s tools/truth -p 'test_*.py' 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    Pop-Location
    $tail = (($t | Select-Object -Last 3) -join ' | ')
    if ($code -ne 0) { Say "HOLD: tools/truth tests fail: $tail"; exit 0 }
    Say "TESTS: $tail"
}
$subjects = (& git -C $Clone log --no-merges --format='%h %s' origin/main..HEAD) -join ' ; '
if ((G push origin HEAD:main) -ne 0) {
    if ((G fetch --quiet origin) -ne 0 -or (G merge --no-edit origin/main) -ne 0 -or (G push origin HEAD:main) -ne 0) {
        G merge --abort | Out-Null
        Say "HOLD: push failed: $GitOut"
        exit 1
    }
}
Say "PUSHED $ahead commit(s): $subjects"
