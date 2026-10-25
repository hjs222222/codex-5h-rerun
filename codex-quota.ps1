<#
codex-quota.ps1 — 抓取 Codex 本地额度状态（无需账号 API、无需第三方工具）

数据源：~/.codex/sessions/**/rollout-*.jsonl（Codex CLI 自己写入的会话事件流）
  · token_count 事件            → 服务端用量快照：payload.rate_limits（plan/额度桶/credits）+ 会话累计 token
  · task_complete + usage_limit_exceeded → 到限事件 + 服务端给出的恢复时间
  · token_usage_record 事件      → 滚动 5h 窗口用量的本地估算

用法：pwsh -File codex-quota.ps1 [-RecentFiles 20]
#>
param([int]$RecentFiles = 20)

$ErrorActionPreference = 'Stop'
$sessions = Join-Path $env:USERPROFILE '.codex\sessions'
if (-not (Test-Path -LiteralPath $sessions)) { Write-Error "未找到会话目录: $sessions（请先运行过 codex）"; exit 1 }

$files = Get-ChildItem -LiteralPath $sessions -Recurse -Filter 'rollout-*.jsonl' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First $RecentFiles
if (-not $files) { Write-Error '未找到 rollout 文件'; exit 1 }

$snap = $null   # 最新且有完整 rate_limits 的 token_count 快照
$hit  = $null   # 最新到限错误
$now  = Get-Date
$cut  = $now.ToUniversalTime().AddHours(-5)
$estTurns = 0; $estTokens = 0L   # 滚动 5h 窗口估算

foreach ($f in $files) {
  foreach ($line in Get-Content -LiteralPath $f.FullName -Encoding utf8) {
    if ($line.Length -lt 20) { continue }
    try {
      if ($line -match '"type":"token_count"') {
        $e = $line | ConvertFrom-Json
        $rl = $e.payload.rate_limits
        if (-not $snap -or $e.timestamp -gt $snap.timestamp) { $snap = $e }   # 每个 token_count 都带 rate_limits，取最新即可
      } elseif ($line -match 'usage_limit_exceeded') {
        $e = $line | ConvertFrom-Json
        if (-not $hit -or $e.timestamp -gt $hit.timestamp) { $hit = $e }
      } elseif ($line -match '"type":"token_usage_record"') {
        $e = $line | ConvertFrom-Json
        if ([datetime]$e.timestamp -ge $cut) {
          $estTurns++
          $estTokens += [long]$e.payload.usage.total_tokens
        }
      }
    } catch { continue }   # 跳过损坏行
  }
}

'==== Codex 额度状态 ===='
'检查时间 : {0:yyyy-MM-dd HH:mm:ss}（本地）' -f $now
$codexProc = Get-Process codex -ErrorAction SilentlyContinue
'codex进程 : ' + $(if ($codexProc) { '运行中 (PID ' + ($codexProc.Id -join ',') + ')' } else { '未运行' })

if ($snap) {
  $rl = $snap.payload.rate_limits
  $tu = $snap.payload.info.total_token_usage
  ''
  '[服务端用量快照] 事件时间 {0}' -f $snap.timestamp
  '  套餐(plan_type) : {0}' -f $(if ($rl.plan_type) { $rl.plan_type } else { '—（该快照未带此字段）' })
  '  额度桶(limit_id): {0}   credits: has={1} unlimited={2} balance={3}' -f `
      $(if ($rl.limit_id) { $rl.limit_id } else { '—' }), `
      $(if ($null -ne $rl.credits.has_credits) { $rl.credits.has_credits } else { '—' }), `
      $(if ($null -ne $rl.credits.unlimited) { $rl.credits.unlimited } else { '—' }), `
      $(if ($null -ne $rl.credits.balance) { $rl.credits.balance } else { '—' })
  '  会话累计 token  : input={0:N0} cached={1:N0} output={2:N0} reasoning={3:N0} total={4:N0}' -f $tu.input_tokens, $tu.cached_input_tokens, $tu.output_tokens, $tu.reasoning_output_tokens, $tu.total_tokens
} else { ''; '[快照] 最近 {0} 个 rollout 中未找到 token_count 事件' -f $RecentFiles }

if ($hit) {
  ''
  '[最近到限] 事件时间 {0}' -f $hit.timestamp
  '  错误类型 : {0}' -f $hit.payload.error.codex_error_info
  '  服务端提示: {0}' -f $hit.payload.error.message
} else { ''; '[到限] 最近 {0} 个 rollout 中未发现 usage_limit_exceeded 记录' -f $RecentFiles }

''
'[滚动5h窗口估算] 最近 5 小时内 token_usage_record：{0} 条，累计 total_tokens≈{1:N0}（本地估算，以服务端口径为准）' -f $estTurns, $estTokens
