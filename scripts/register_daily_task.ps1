<#
.SYNOPSIS
    Register (or re-register) the "PhoneLoop eBay Daily" scheduled task.

.DESCRIPTION
    DRY RUN BY DEFAULT. Prints the exact absolute action path it would bake
    into the task and changes nothing unless -Force is passed.

    RUN THIS FROM THE MAIN CHECKOUT ONLY. A task registered from a git
    worktree bakes in a path that is deleted when the worktree is cleaned
    up, and Task Scheduler then fails every night with a "cannot find the
    file" that nobody sees -- the whole failure this deliverable exists to
    prevent, in the tool that is supposed to prevent it. Three independent
    guards below refuse that, and -Force does NOT override them.

    Two triggers, deliberately, and this is a DEVIATION from every other
    scheduled task on this machine (all of which have
    StartWhenAvailable=False):

      * Daily 03:00 local, with -StartWhenAvailable so a missed window is
        caught up rather than silently skipped.
      * AtLogOn with a 10-minute delay, unless -NoLogonTrigger.

    Justification: this machine is a laptop that is frequently off at 03:00
    (boot events 28/08 09:22, 27/08 19:06, 22/08 21:20). A 03:00-only
    trigger with StartWhenAvailable=False loses those nights entirely, with
    no record anywhere -- and Tier 3 needs consecutive dates, so a lost
    night is the whole cost of the schedule. nightly.py's day-guard makes
    the extra trigger safe: it sweeps at most once per UTC day.

    THE HONEST COST OF THE LOGON TRIGGER: neither bulk_ebay.py call in
    run_daily.cmd passes --headless (it defaults False), so the sweep drives
    a VISIBLE Chrome for about 20 minutes. A logon-triggered catch-up will open
    a browser window while you are working. Pass -NoLogonTrigger if that is
    not acceptable. Do NOT "fix" it with a stored password / run-whether-
    logged-on-or-not instead: headless has never been measured against eBay
    here, and the only headless measurement in this repo
    (scripts/refresh_cex.py) earns a Cloudflare 403.

    DisallowStartIfOnBatteries is left at its default $true, matching every
    other task on this box. An unplug mid-run stops the task, which leaves
    the heartbeat stuck at state="running" -- and phoneloop.heartbeat.assess
    reads that as ALARM the next morning, which is the correct outcome.

.PARAMETER Force
    Actually register. Without it this script only prints what it would do.

.PARAMETER NoLogonTrigger
    Daily 03:00 only. No catch-up on logon, no surprise browser window.

.PARAMETER Status
    Print the current task state and the nightly health assessment, and exit.

.PARAMETER Unregister
    Remove the task. Requires -Force.

.EXAMPLE
    .\scripts\register_daily_task.ps1
    .\scripts\register_daily_task.ps1 -Force
    .\scripts\register_daily_task.ps1 -Status

.NOTES
    Non-elevated: RunLevel Limited, LogonType Interactive. Six user tasks
    already run this way on this box; only /rl HIGHEST would need elevation.
    PowerShell 5.1 on this machine -- no &&, no ternary, no ?? below.
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$NoLogonTrigger,
    [switch]$Status,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$TaskName    = 'PhoneLoop eBay Daily'
$RepoRoot    = Split-Path -Parent $PSScriptRoot
$ActionPath  = Join-Path $PSScriptRoot 'run_daily_scheduled.cmd'
$DataDir     = Join-Path $env:USERPROFILE '.phoneloop\prod'
$PinnedPy    = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'

function Write-Section($text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Cyan
}

# --- guard: never register from a worktree ----------------------------------
# Three independent checks. Any ONE of them firing is a refusal, and -Force
# does not override -- the point is that a worktree path LOOKS fine and then
# evaporates, so a human eyeballing the path is not a sufficient guard.
function Test-IsWorktree {
    $reasons = @()

    # 1. In a linked worktree, .git is a FILE containing "gitdir: ...".
    #    In the main checkout it is a directory.
    $dotGit = Join-Path $RepoRoot '.git'
    if (Test-Path $dotGit -PathType Leaf) {
        $reasons += ".git is a FILE (a linked worktree), not a directory"
    }

    # 2. --git-dir and --git-common-dir diverge only in a worktree.
    #    Verified in the main checkout: both return ".git".
    Push-Location $RepoRoot
    try {
        $gitDir    = (& git rev-parse --git-dir 2>$null)
        $commonDir = (& git rev-parse --git-common-dir 2>$null)
        if ($gitDir -and $commonDir -and ($gitDir -ne $commonDir)) {
            $reasons += "git-dir '$gitDir' differs from git-common-dir '$commonDir'"
        }
    } finally {
        Pop-Location
    }

    # 3. The path itself smells temporary.
    if ($RepoRoot -match '\\Temp\\|\\worktrees?\\') {
        $reasons += "repo path matches a temporary/worktree pattern"
    }

    return $reasons
}

# --- status -------------------------------------------------------------------
if ($Status) {
    Write-Section "Task"
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "  '$TaskName' is NOT registered." -ForegroundColor Yellow
    } else {
        $task | Select-Object TaskName, State | Format-List
        Get-ScheduledTaskInfo -TaskName $TaskName |
            Select-Object LastRunTime, LastTaskResult, NextRunTime,
                          NumberOfMissedRuns | Format-List
        Write-Host "  Triggers:"
        foreach ($t in $task.Triggers) { Write-Host "    $($t.CimClass.CimClassName)  $($t.StartBoundary)" }
        Write-Host "  Action:  $($task.Actions[0].Execute)"
    }

    Write-Section "Nightly health (heartbeat + store, not the heartbeat alone)"
    if (Test-Path $PinnedPy) {
        $env:PHONELOOP_DATA_DIR = $DataDir
        & $PinnedPy (Join-Path $PSScriptRoot 'sweep_status.py')
        Write-Host "  (exit $LASTEXITCODE -- 1 means ALARM)"
    } else {
        Write-Host "  installer python not found at $PinnedPy" -ForegroundColor Red
    }
    exit 0
}

# --- unregister ----------------------------------------------------------------
if ($Unregister) {
    if (-not $Force) {
        Write-Host "DRY RUN: would unregister '$TaskName'. Re-run with -Force."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered '$TaskName'." -ForegroundColor Green
    exit 0
}

# --- preflight -----------------------------------------------------------------
Write-Section "Preflight"
Write-Host "  Repo root    $RepoRoot"
Write-Host "  ACTION PATH  $ActionPath"
Write-Host "  Working dir  $RepoRoot"
Write-Host "  Data dir     $DataDir  (set inside run_daily.cmd, not here --"
Write-Host "                a Task Scheduler action carries no environment)"
Write-Host "  Principal    $env:USERDOMAIN\$env:USERNAME (Interactive, Limited)"

$worktreeReasons = Test-IsWorktree
if ($worktreeReasons.Count -gt 0) {
    Write-Host ''
    Write-Host "REFUSING: this is not the main checkout." -ForegroundColor Red
    foreach ($r in $worktreeReasons) { Write-Host "  - $r" -ForegroundColor Red }
    Write-Host ''
    Write-Host "A task registered here would bake in a path that is deleted"
    Write-Host "when the worktree is cleaned up, and every night would then"
    Write-Host "fail silently. Run this from the main checkout instead."
    exit 2
}

if (-not (Test-Path $ActionPath)) {
    Write-Host "REFUSING: action not found at $ActionPath" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $PinnedPy)) {
    Write-Host "REFUSING: installer python not found at $PinnedPy" -ForegroundColor Red
    Write-Host "The MSIX/WindowsApps build exits SILENTLY under Task Scheduler."
    exit 2
}
if (-not (Test-Path $DataDir)) {
    Write-Host ''
    Write-Host "WARNING: $DataDir does not exist yet." -ForegroundColor Yellow
    Write-Host "run_daily.cmd defaults PHONELOOP_DATA_DIR there. Until the"
    Write-Host "eBay snapshot corpus is copied in, the first scheduled night"
    Write-Host "sweeps into an EMPTY store and Tier 3 stays switched off:"
    Write-Host "  robocopy `"$env:USERPROFILE\.phoneloop\e2e_clean`" `"$DataDir`" /E"
}

# --- build ----------------------------------------------------------------------
$action = New-ScheduledTaskAction -Execute $ActionPath -WorkingDirectory $RepoRoot

$triggers = @()
$triggers += New-ScheduledTaskTrigger -Daily -At '03:00'
if (-not $NoLogonTrigger) {
    $logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $logon.Delay = 'PT10M'
    $triggers += $logon
}

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# MultipleInstances IgnoreNew is belt-and-braces on top of nightly.py's own
# lock: the lock is what actually protects duplicated eBay egress and the
# shared checkpoint, because it also covers a hand-run alongside the task,
# which Task Scheduler cannot see.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

Write-Section "Triggers"
Write-Host "  Daily 03:00 local, StartWhenAvailable (catches a missed window)"
if ($NoLogonTrigger) {
    Write-Host "  (no logon trigger -- a night the machine is off is LOST)"
} else {
    Write-Host "  AtLogOn +10min -- catch-up. Opens a VISIBLE Chrome for ~20 min."
    Write-Host "  nightly.py's day-guard means at most one sweep per UTC day."
}

if (-not $Force) {
    Write-Host ''
    Write-Host "DRY RUN -- nothing registered. Re-run with -Force to apply:" -ForegroundColor Yellow
    Write-Host "  .\scripts\register_daily_task.ps1 -Force"
    exit 0
}

# -Force on Register-ScheduledTask is what makes re-registration idempotent:
# it replaces an existing task of the same name rather than erroring.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host ''
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host ''
Write-Host "Next, while you are AT the machine (the sweep is headed, ~20 min):"
Write-Host "  schtasks /run /tn `"$TaskName`""
Write-Host "That runs under the task's own principal, which is the only honest"
Write-Host "test of %LOCALAPPDATA% resolution, PHONELOOP_DATA_DIR, the pinned"
Write-Host "Chrome path and the Mullvad split-tunnel exemption."
Write-Host ''
Write-Host "Then check it landed:"
Write-Host "  .\scripts\register_daily_task.ps1 -Status"
exit 0
