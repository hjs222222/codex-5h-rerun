# Codex 5h 限额看门狗（codex-watch）

自动监控 [Codex CLI](https://github.com/openai/codex) 的 **5 小时滚动限额**，到点后自动恢复（resume）被限额打断的对话，并在一个彩色桌面窗口里实时展示额度、恢复倒计时、用量曲线和事件日志。

> 为什么需要它：Codex 的 Plus 套餐有滚动 5 小时的 token/计算额度，跑大任务时经常"干到一半被限额打断，还要记着等恢复再手动 resume"。这个看门狗替你盯着，恢复后自动续跑。

## 功能特性

- 🎛 **彩色桌面看板**（Tkinter，无需浏览器）：额度状态、恢复倒计时、滚动 5h 用量曲线、事件日志、进程状态一览
- ⏱ **自动续跑开关**：到点自动执行 `codex exec resume`，可随时在主界面一键开关（绿色=开 / 灰色=关）
- 🔢 **自动续跑次数上限**：每个到限周期最多自动尝试 N 次（0 = 不限制，失败持续重试），成功即停，不会无限烧额度
- 🎯 **指定会话续跑**：弹窗列出最近的 Codex 对话（显示你在 Codex 里起的名字，如 `slidecrew01`），双击即可设为续跑目标；绿色 = 当前目标
- 🪟 **续跑过程可见**：续跑时弹出真实终端窗口，实时看到 Codex 干活
- ⚙ **设置即改即生效**：轮询间隔（5–1800 秒）、自动续跑提示语、开关、次数上限，保存后无需重启
- 📜 **滚动 5h 用量统计**：从 Codex 会话 rollout 中读取真实 token 用量，估算恢复时间（以服务端 `try again at` 时间为准）

## 文件结构

```
codex-watch.py          主程序（额度监控 + 自动续跑 + 彩色 GUI）
codex-quota.ps1         命令行额度抓取脚本（PowerShell）
启动Codex看门狗.bat     双击启动器（使用无窗口 pythonw 运行）
codex-watch-config.json 运行配置（由设置界面自动生成/改写）
codex-watch.log         运行日志
```

## 运行要求

- Windows 10/11
- Python 3.10+（带 `tkinter`；建议 Anaconda 版 `pythonw.exe`）
- 已安装并登录 [Codex CLI](https://github.com/openai/codex)（`codex` 在 PATH 中）

## 快速开始

1. 把 `codex-watch.py` 放到任意目录。
2. 修改 `启动Codex看门狗.bat` 里的 python 路径（如有必要）。
3. 双击 `启动Codex看门狗.bat`（或 `pythonw codex-watch.py`）。
4. 主界面点 **🎯 指定会话…** 选择要续跑的对话（也可直接双击），或保持"最近会话"。

## 配置说明（`codex-watch-config.json`）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `poll_interval_sec` | 15 | 轮询间隔（秒），范围 5–1800（最长 30 分钟一次） |
| `resume_prompt` | 继续上次未完成的任务… | 自动续跑时附带给 Codex 的提示语，可多行 |
| `auto_resume` | true | 自动续跑总开关 |
| `auto_resume_max` | 3 | 每个到限周期自动续跑次数上限，0 = 不限制 |
| `resume_target` | last | `last` = 续最近会话，`custom` = 续指定会话 |
| `resume_session_id` | "" | 指定会话的 ID（`resume_target=custom` 时生效） |
| `resume_extra_args` | `--skip-git-repo-check` | 传给 `codex exec resume` 的额外参数 |

## 工作原理

- 扫描 `~/.codex/sessions/` 下的 rollout 记录（JSONL），从 `token_count` 事件读取各限额桶的余额；
- 命中 `usage_limit_exceeded` 时，解析服务端给出的恢复时间（`try again at ...`）并进入倒计时；
- 到点后执行 `codex exec resume <session_id> <prompt>`（弹出一个可见终端窗口运行）；
- 续跑成功自动停止；失败按 `auto_resume_max` 次数重试，超限写日志提醒。

## 注意事项

- **不要**让交互式 `codex` 会话和看门狗同时续跑同一个会话（同一 rollout 文件/工作目录会写冲突）；到点前关掉你手动开着的同名会话窗口即可。
- Codex 会话是持久化的，关掉终端不会丢进度。
- 若续跑任务中途需要人工批准（approval），后台会等待输入，留意日志或弹出的终端窗口。
- 当前实现面向 Windows；核心逻辑与路径在 `codex-watch.py` 顶部可调整。

## License

MIT
