#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex-watch.py — Codex 5h 限额看门狗（本地）+ 中文彩色桌面看板
================================================================
做什么：
  1. 增量读取 ~/.codex/sessions/**/rollout-*.jsonl（Codex 自己写的会话事件流）
  2. 抓取服务端额度快照（token_count 里的 plan / 额度桶 / credits）
  3. 检测到限事件（task_complete + usage_limit_exceeded），记录服务端给的恢复时间
  4. 统计滚动 5h 窗口用量（token_usage_record）
  5. 到点自动执行 `codex exec resume <session_id> "继续…"` 续跑对话
  6. 全部状态显示在一个彩色中文桌面窗口里

用法：
  python codex-watch.py            # 打开桌面看板
  python codex-watch.py --probe    # 只抓一次状态打印到终端（无窗口，用于调试/脚本）
配置文件：codex-watch-config.json（与脚本同目录，默认自动生成）

依赖：仅 Python 标准库（tkinter / threading / queue / subprocess）。
"""
import os
import sys
import json
import re
import time
import queue
import socket
import threading
import subprocess
import tkinter as tk
import tkinter.messagebox
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
APP_TITLE = "Codex 5h 限额看门狗"
SESSION_DIR = Path(os.environ.get("USERPROFILE", "~")).expanduser() / ".codex" / "sessions"
CONFIG_PATH = Path(__file__).resolve().parent / "codex-watch-config.json"
LOG_PATH = Path(__file__).resolve().parent / "codex-watch.log"

WINDOW_SECONDS = 5 * 3600          # 滚动 5h 窗口
DEFAULT_POLL = 30                  # 事件轮询间隔（秒），unsnooze 同款 30s tick
DEFAULT_PROMPT = "继续上次未完成的任务，从断点接着做。"
DEFAULT_EXTRA = ["--skip-git-repo-check"]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CREATE_NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0

# 看板配色（暗色 + 高饱和）
BG        = "#0f172a"
CARD_BG   = "#1e293b"
CARD_EDGE = "#334155"
TEXT      = "#e2e8f0"
MUTED     = "#94a3b8"
TEAL      = "#2dd4bf"
BLUE      = "#60a5fa"
AMBER     = "#fbbf24"
GREEN     = "#4ade80"
RED       = "#f87171"
PURPLE    = "#c084fc"
PINK      = "#f472b6"
CYAN      = "#22d3ee"


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def load_config():
    cfg = {
        "poll_interval_sec": DEFAULT_POLL,
        "resume_prompt": DEFAULT_PROMPT,
        "resume_extra_args": list(DEFAULT_EXTRA),
        "auto_resume": True,
        "auto_resume_max": 3,             # 每个到限周期内自动续跑次数上限，0 = 不限制
        "resume_target": "last",          # "last"=最近会话 | "custom"=指定会话
        "resume_session_id": "",
        "sessions_dir": str(SESSION_DIR),
    }
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except Exception:
        pass
    # 类型/范围校验：防止旧配置写入 None/非法值导致界面或保存逻辑异常
    try:
        cfg["poll_interval_sec"] = max(5, min(1800, int(cfg["poll_interval_sec"])))
    except Exception:
        cfg["poll_interval_sec"] = DEFAULT_POLL
    cfg["auto_resume"] = bool(cfg.get("auto_resume", True))
    try:
        cfg["auto_resume_max"] = max(0, int(cfg.get("auto_resume_max", 3)))
    except Exception:
        cfg["auto_resume_max"] = 3
    if cfg.get("resume_target") not in ("last", "custom"):
        cfg["resume_target"] = "last"
    if not isinstance(cfg.get("resume_session_id"), str):
        cfg["resume_session_id"] = ""
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 核心状态
# ----------------------------------------------------------------------------
class Core:
    """看门狗核心：增量读 rollout → 状态机 → 自动续跑。GUI 只读它的状态。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.log_q = queue.Queue()
        self.ready = False

        self.snap = {}                 # 最新 token_count 快照
        self.hit = None                # 最新到限事件 {ts, message, session_id, cwd}
        self.rolling = deque()         # (epoch_utc, total_tokens)
        self.rolling_max = 0
        self.percent_pts = deque()     # (epoch_utc, used5h_percent) —— 实时监测的消耗点序列
        self.limits = {}              # limit_id -> 该桶最新 rate_limits 聚合（5h/周/credits）
        self.process_pids = []
        self.status = "ok"             # ok | limited | waiting | resuming | error
        self.resume_started = 0        # 时间戳（避免同一 hit 重复续跑）
        self._auto_attempts = 0        # 当前到限周期已自动续跑次数
        self._auto_cycle_key = ""      # 当前到限事件标识（变化=新周期）
        self._auto_succeeded = False   # 本轮自动续跑已成功
        self._auto_max_warned = False  # 已达上限提示是否已发
        self.resume_done = False
        self.resume_log = ""

        self._stop = threading.Event()
        self._scan_again = threading.Event()
        self._resume_req = None        # 手动续跑请求: "latest" | session_id
        self._kill_req = threading.Event()

        self._reader = RolloutReader(self.cfg["sessions_dir"])
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        self.ready = True

    # ---------- 日志 ----------
    def log(self, level, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_q.put((ts, level, text))
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{level}] {text}\n")
        except Exception:
            pass

    # ---------- 事件处理 ----------
    def on_event(self, path, evt):
        etype = evt.get("type")
        payload = evt.get("payload") or {}
        try:
            ts_epoch = datetime.fromisoformat(evt["timestamp"].replace("Z", "+00:00")).timestamp()
        except Exception:
            ts_epoch = time.time()

        if etype == "session_meta":
            # 记录该文件的会话 id 与工作目录，供续跑使用
            self._reader.set_meta(path, payload.get("session_id"), payload.get("cwd"))

        elif etype == "event_msg" and payload.get("type") == "token_count":
            ts = evt.get("timestamp") or ""
            info = payload.get("info") or {}
            rl = payload.get("rate_limits") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens", 0)
            self._apply_limits(rl, total, ts, ts_epoch)



        elif etype == "event_msg" and payload.get("type") == "task_complete":
            err = payload.get("error") or {}
            if err.get("codex_error_info") == "usage_limit_exceeded":
                ts = evt.get("timestamp") or ""
                sid, cwd = self._reader.meta_of(path)
                msg = err.get("message", "")
                with self.lock:
                    if self.hit and ts <= self.hit.get("ts", ""):
                        return                # 只保留时间戳最新的到限事件
                    self.hit = {"ts": ts, "message": msg, "session_id": sid, "cwd": cwd}
                    self.status = "limited"
                    self.resume_done = False
                self.log("LIMIT", f"到限！会话 {sid} · {msg}")

        elif etype == "token_usage_record":
            usage = payload.get("usage") or {}
            total = int(usage.get("total_tokens") or 0)
            with self.lock:
                self.rolling.append((ts_epoch, total))
                self._prune_rolling()
                self.rolling_max = max(self.rolling_max, total)

    def _apply_limits(self, rl, total, ts, evt_ts):
        """按 limit_id 分桶聚合最新额度。0%/None 防污染：同一 5h 窗口（resets5h 相同）
        且新值为无效/0 时忽略（Codex 有时会写入窗口未变的 0% 事件）；窗口回退保护：
        resets5h 早于当前值的旧窗口事件一律丢弃。"""
        cred = rl.get("credits") or {}
        p = rl.get("primary") or {}
        s_ = rl.get("secondary") or {}
        lid = rl.get("limit_id") or "?"
        pct5, reset5 = p.get("used_percent"), p.get("resets_at")
        pctw, resetw = s_.get("used_percent"), s_.get("resets_at")
        new = {
            "ts": ts, "plan": rl.get("plan_type"), "limit_id": lid,
            "credits_has": cred.get("has_credits"),
            "credits_unlimited": cred.get("unlimited"),
            "credits_balance": cred.get("balance"),
            "total": total,
            "used5h": pct5, "resets5h": reset5,
            "used_week": pctw, "resets_week": resetw,
            "used_percent": pct5, "resets_at": reset5,   # 兼容旧字段
        }
        changed = False
        with self.lock:
            cur = self.limits.get(lid)
            if cur and ts <= cur.get("ts", ""):
                return                              # 只保留时间戳最新的
            # 同窗口判定：resets_at 服务端每秒漂移，用 ±1h 容忍（窗口重置是 5h 级）
            same_window = False
            if cur and reset5 and cur.get("resets5h"):
                try:
                    same_window = abs(int(reset5) - int(cur.get("resets5h"))) < 3600
                except Exception:
                    same_window = False
            if cur and same_window and not isinstance(pct5, (int, float)):
                return                              # 同窗口且无百分比 → 忽略垃圾事件
            if cur and same_window and isinstance(pct5, (int, float)) and pct5 <= 0:
                return                              # 同窗口 0% 垃圾事件（窗口未重置却报 0）
            changed = (not cur or cur.get("used5h") != pct5 or cur.get("used_week") != pctw)
            self.limits[lid] = new
            self.snap = dict(new)                   # 兼容旧 GUI/CLI 字段
            # 消耗点序列（主桶 5h%）：回落（跨窗口重置）则清空重计；去重防噪声
            if isinstance(pct5, (int, float)):
                if self.percent_pts and pct5 < (self.percent_pts[-1][1] - 5.0):
                    self.percent_pts.clear()
                last_t = self.percent_pts[-1][0] if self.percent_pts else 0.0
                if evt_ts - last_t >= 5.0:
                    self.percent_pts.append((evt_ts, float(pct5)))
                    while len(self.percent_pts) > 120:
                        self.percent_pts.popleft()
        if changed:
            self.log("INFO", f"额度更新：桶={lid} 5h={pct5}% 周={pctw}% credits={new['credits_balance']}")

    def main_limit_id(self):
        """账号主额度桶：优先 codex（plus 计划主要配额桶）。"""
        with self.lock:
            if not self.limits:
                return None
            if "codex" in self.limits:
                return "codex"
            return max(self.limits, key=lambda k: (self.limits[k].get("resets5h") or 0))

    def main_limits(self):
        lid = self.main_limit_id()
        with self.lock:
            return dict(self.limits.get(lid) or {}) if lid else {}

    def reset_time(self, hit):
        """权威恢复点：优先用主桶 rate_limits.resets_at（epoch，无时区歧义），
        其次解析 "try again at ..." 文本，最后兜底按滚动窗口估算。"""
        m = self.main_limits()
        ra = m.get("resets_at")
        if ra:
            try:
                return datetime.fromtimestamp(int(ra))
            except Exception:
                pass
        return (self.parse_reset(hit["message"], self._epoch(hit["ts"]))
                or self.estimate_reset())

    def _prune_rolling(self):
        cut = time.time() - WINDOW_SECONDS
        while self.rolling and self.rolling[0][0] < cut:
            self.rolling.popleft()

    # ---------- 进程检查 ----------
    def refresh_process(self):
        pids = []
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True,
                                 encoding="gbk", errors="replace", timeout=10,
                                 creationflags=CREATE_NO_WINDOW).stdout
            for ln in out.splitlines():
                parts = ln.strip().strip('"').split('","')
                if len(parts) >= 2 and "codex" in parts[0].lower():
                    pids.append(parts[1])
        except Exception:
            pass
        with self.lock:
            self.process_pids = pids

    # ---------- 恢复时间 ----------
    RESET_RE = re.compile(r"try again at\s+(.+?)\.?\s*$", re.I)
    MONTH_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})?\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)

    def parse_reset(self, message, hit_epoch):
        """从服务端提示里解析恢复时间；解析失败返回 None。"""
        if not message:
            return None
        m = self.RESET_RE.search(message)
        if not m:
            return None
        text = m.group(1).strip()
        # 形式1: Oct 26th, 2026 12:22 AM
        mm = self.MONTH_RE.search(text)
        if mm:
            mon, day, year, hm = mm.group(1), int(mm.group(2)), mm.group(3), mm.group(4)
            year = int(year) if year else datetime.fromtimestamp(hit_epoch).year
            try:
                return datetime.strptime(f"{mon} {day} {year} {hm}", "%b %d %Y %I:%M %p")
            except ValueError:
                return None
        # 形式2: 6:02 PM（无日期 → 按事件当天，早于事件则视为次日）
        try:
            t = datetime.strptime(text, "%I:%M %p").time()
        except ValueError:
            try:
                t = datetime.strptime(text, "%I:%M %P").time()
            except ValueError:
                return None
        base = datetime.fromtimestamp(hit_epoch)
        dt = datetime.combine(base.date(), t)
        if dt < base:
            dt += timedelta(days=1)
        return dt

    def estimate_reset(self):
        """无服务端时间时的兜底估算：窗口内最早记录 + 5h。"""
        with self.lock:
            if self.rolling:
                return datetime.fromtimestamp(self.rolling[0][0] + WINDOW_SECONDS)
            if self.snap:
                try:
                    base = datetime.fromisoformat(self.snap["ts"].replace("Z", "+00:00")).replace(tzinfo=None)
                    return base.replace(tzinfo=None) + timedelta(hours=5)
                except Exception:
                    pass
        return None

    # ---------- 看门狗主循环 ----------
    def _loop(self):
        self.log("INFO", "看门狗已启动")
        while not self._stop.is_set():
            try:
                self._reader.scan(self.on_event)
                self.refresh_process()
                self._maybe_auto_resume()
                self._handle_requests()
            except Exception as e:
                self.log("ERROR", f"轮询异常：{e}")
            self._scan_again.wait(self.cfg["poll_interval_sec"])
            self._scan_again.clear()

    def _handle_requests(self):
        req = self._resume_req
        if req is not None:
            self._resume_req = None
            self.start_resume(self.resolve_target(req or "latest"))
        if self._kill_req.is_set():
            self._kill_req.clear()
            self.kill_codex()

    def resolve_target(self, fallback):
        """续跑目标：配置为 custom 且指定了会话 ID 时，用指定会话；否则用 fallback。"""
        c = self.cfg
        if c.get("resume_target") == "custom" and c.get("resume_session_id"):
            return c["resume_session_id"]
        return fallback


    # ---------- 实时额度：消耗速率与到限预测 ----------
    def burn_metrics(self):
        """参考 unsnooze usage：用窗口用量百分比时间序列算消耗速率（%/min）
        与按当前速率距触顶的 ETA 区间（band，估算值）。返回 dict 或 None。"""
        m = self.main_limits()
        with self.lock:
            pts = list(self.percent_pts)
        pct_now = m.get("used5h") if m else None
        snap_ts = m.get("ts") if m else None
        rates = []
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt < 60 or dt > 2700:          # 剔除 <1min 噪声与 >45min 空闲 gap
                continue
            rates.append((v1 - v0) / (dt / 60.0))
        if not rates:
            return {"per_min": None, "lo": None, "hi": None, "pct_now": pct_now,
                    "points": len(pts), "snap_ts": snap_ts, "age_s": None}
        recent = rates[-6:] if len(rates) >= 6 else rates
        lo, hi = min(recent), max(recent)
        return {
            "per_min": sum(recent) / len(recent),
            "lo": lo, "hi": hi,
            "pct_now": pct_now,
            "points": len(pts),
            "snap_ts": snap_ts,
            "age_s": (time.time() - self._epoch(snap_ts)) if snap_ts else None,
        }

    def eta_band(self, metrics):
        """由速率区间推出距到限的 ETA 区间（分钟）。数据不足时返回 None。"""
        if not metrics or not metrics["per_min"] or metrics["per_min"] <= 0:
            return None
        pct = metrics["pct_now"]
        if pct is None:
            return None
        remain_pct = max(0.0, 100.0 - float(pct))
        if remain_pct <= 0:
            return (0, 0)
        hi_min = remain_pct / max(metrics["lo"], 1e-9) if metrics["lo"] > 0 else None
        lo_min = remain_pct / max(metrics["hi"], 1e-9) if metrics["hi"] > 0 else None
        if lo_min is None or hi_min is None:
            return None
        lo_min, hi_min = min(lo_min, hi_min), max(lo_min, hi_min)
        return (lo_min, hi_min)

    def _maybe_auto_resume(self):
        if not self.cfg["auto_resume"]:
            return
        with self.lock:
            hit = self.hit
            status = self.status
        if not hit:
            return
        # 到限事件变化 → 新周期，重置自动续跑计数
        key = hit.get("ts") or ""
        if key != self._auto_cycle_key:
            self._auto_cycle_key = key
            self._auto_attempts = 0
            self._auto_succeeded = False
            self._auto_max_warned = False
        if status in ("resuming", "probing") or self._auto_succeeded:
            return
        mx = int(self.cfg.get("auto_resume_max") or 0)
        if 0 < mx <= self._auto_attempts:
            if not self._auto_max_warned:
                self._auto_max_warned = True
                self.log("WARN", f"已达自动续跑次数上限（{mx} 次），不再自动续跑；可点“手动续跑”或在设置中调大次数")
            return
        reset = self.reset_time(hit)
        if reset and time.time() >= reset.timestamp():
            self._auto_attempts += 1
            self.log("INFO", f"恢复点已到，先探测额度（第 {self._auto_attempts} 次" + ("" if mx <= 0 else f"/上限 {mx} 次") + "）")
            with self.lock:
                self.status = "probing"
            threading.Thread(target=self._probe_and_resume, args=(hit,), daemon=True).start()

    def _probe_once(self, cwd):
        """静默探测额度：跑一个轻量 codex exec，成功=额度已恢复。不弹窗。"""
        try:
            p = subprocess.Popen(
                ["codex", "exec", "--skip-git-repo-check",
                 "额度探测：请只回复“OK”，不要执行任何操作。"],
                cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                out, _ = p.communicate(timeout=150)
            except subprocess.TimeoutExpired:
                p.kill()
                self.log("WARN", "探测超时（150s），视为未恢复")
                return False
            low = out.lower()
            if p.returncode == 0 and "usage limit" not in low and "usage_limit" not in low:
                return True
            self.log("RESUME", f"探测未通过（退出码 {p.returncode}），服务端片段：{out[-180:].strip()}")
            return False
        except Exception as e:
            self.log("ERROR", f"探测异常：{e}")
            return False

    def _probe_and_resume(self, hit):
        """探测成功才续跑；失败每 60 秒重试，直到成功或达上限。"""
        cwd = hit.get("cwd") or None
        sid = self.resolve_target(hit.get("session_id") or "latest")
        while True:
            if self._probe_once(cwd):
                self.log("OK", "额度已恢复，开始自动续跑")
                self.start_resume(sid)
                return
            mx = int(self.cfg.get("auto_resume_max") or 0)
            if 0 < mx <= self._auto_attempts:
                self.log("WARN", f"已达自动续跑探测上限（{mx} 次），停止自动续跑；可点“手动续跑”或调大次数")
                with self.lock:
                    self.status = "error"
                return
            self._auto_attempts += 1
            self.log("WARN", f"额度尚未恢复，60 秒后重试探测（已试 {self._auto_attempts} 次" + ("" if mx <= 0 else f"/上限 {mx} 次") + "）")
            for _ in range(12):
                time.sleep(5)
                with self.lock:
                    if self._auto_succeeded or self._stop.is_set():
                        return

    @staticmethod
    def _epoch(iso):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()

    # ---------- 续跑 ----------
    def start_resume(self, target):
        """target: session_id 或 'latest'。在后台线程执行 codex exec resume。"""
        with self.lock:
            self.status = "resuming"
            self.resume_started = time.time()
            hit = self.hit
        cwd = (hit or {}).get("cwd") or None
        sid = target if target != "latest" else None
        cmd = ["codex", "exec", "resume"]
        if sid:
            cmd.append(sid)
        else:
            cmd.append("--last")
        cmd.append(self.cfg["resume_prompt"])
        cmd += self.cfg.get("resume_extra_args") or []
        self.log("RESUME", "启动续跑：" + " ".join(cmd))

        def run():
            try:
                # 用可见的终端窗口运行续跑，实时输出直接显示在窗口里
                proc = subprocess.Popen(
                    cmd, cwd=cwd,
                    creationflags=CREATE_NEW_CONSOLE,
                )
                proc.wait()
                self.log("RESUME", f"续跑进程结束（退出码 {proc.returncode}），过程输出见弹出的终端窗口")
                with self.lock:
                    self.resume_done = True
                    if proc.returncode == 0:
                        self.status = "waiting"          # 等待新事件确认额度恢复
                        self._auto_succeeded = True      # 本轮自动续跑已成功，避免重复触发
                    else:
                        self.status = "error"
            except Exception as e:
                self.log("ERROR", f"续跑启动失败：{e}")
                with self.lock:
                    self.status = "error"

        threading.Thread(target=run, daemon=True).start()

    def request_resume(self, target=None):
        """target: session_id 或 None（None = 按配置 resume_target 决定目标）。"""
        self._resume_req = target
        self._scan_again.set()

    # ---------- 停止 Codex 进程 ----------
    def kill_codex(self):
        with self.lock:
            pids = list(self.process_pids)
        if not pids:
            self.log("INFO", "没有检测到 codex 进程")
            return
        self.log("WARN", "正在结束 codex 进程：" + ", ".join(pids))
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                               text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass

    def request_kill(self):
        self._kill_req.set()
        self._scan_again.set()

    def scan_now(self):
        self.log("INFO", "手动刷新：立即扫描 rollout 增量")
        self._scan_again.set()

    def stop(self):
        self._stop.set()
        self._scan_again.set()


# ----------------------------------------------------------------------------
# 增量读取 rollout
# ----------------------------------------------------------------------------
class RolloutReader:
    def __init__(self, sessions_dir):
        self.sessions_dir = sessions_dir
        self.tracked = {}      # path -> byte offset
        self.meta = {}         # path -> (session_id, cwd)

    def set_meta(self, path, sid, cwd):
        self.meta[path] = (sid or None, cwd or None)

    def meta_of(self, path):
        return self.meta.get(path, (None, None))

    def scan(self, on_event):
        base = Path(self.sessions_dir)
        if not base.exists():
            return
        for p in base.rglob("rollout-*.jsonl"):
            if "archived" in p.parts:
                continue
            path = str(p)
            try:
                size = p.stat().st_size
                offset = self.tracked.get(path, 0)
                if size < offset:                    # 文件被截断/轮转
                    offset = 0
                if size <= offset:
                    continue
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read(size - offset)
                text = data.decode("utf-8", errors="replace")
                if text.endswith("\n"):
                    complete, pending = text.split("\n"), ""
                else:
                    *complete, pending = text.split("\n")
                consumed = len(data) - len(pending.encode("utf-8", errors="replace"))
                self.tracked[path] = offset + consumed
                for ln in complete:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        evt = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(evt, dict):
                        try:
                            on_event(path, evt)
                        except Exception:
                            continue
            except Exception:
                continue


# ----------------------------------------------------------------------------
# 一次性探针模式
# ----------------------------------------------------------------------------
def probe(cfg):
    core = Core(cfg)
    time.sleep(1.0)                      # 等 worker 完成第一轮
    core.scan_now()
    time.sleep(6.0)                      # 等全量扫描完成（首扫要读所有 rollout）
    core.refresh_process()
    with core.lock:
        snap, hit, pids = core.snap, core.hit, list(core.process_pids)
        turns = len(core.rolling)
        tokens = sum(t for _, t in core.rolling)
    print("==== Codex 额度状态（探针） ====")
    print("检查时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("codex进程:", "运行中 (" + ", ".join(pids) + ")" if pids else "未运行")
    if snap:
        print(f"[快照] plan={snap['plan']} 桶={snap['limit_id']} credits={snap['credits_balance']} "
              f"会话累计token={snap['total']:,}  ts={snap['ts']}")
    else:
        print("[快照] 暂无 token_count 事件")
    if hit:
        reset = core.reset_time(hit)
        print(f"[到限] 会话={hit['session_id']} 事件时间={hit['ts']}")
        print(f"  服务端提示: {hit['message']}")
        print(f"  解析恢复时间: {reset}")
    else:
        print("[到限] 未发现 usage_limit_exceeded")
    print(f"[5h估算] {turns} 条用量记录, 累计≈{tokens:,} tokens")
    est = core.estimate_reset()
    print(f"[5h估算恢复] {est}")
    core.stop()
    return 0


# ----------------------------------------------------------------------------
# GUI 看板
# ----------------------------------------------------------------------------
def _card(parent, title, value, accent, row, col, pad=6):
    f = tk.Frame(parent, bg=CARD_BG, highlightbackground=accent, highlightthickness=1, bd=0)
    f.grid(row=row, column=col, sticky="nsew", padx=pad, pady=pad)
    tk.Label(f, text=title, bg=CARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(pady=(10, 2))
    val = tk.Label(f, text=value, bg=CARD_BG, fg=accent, font=("Microsoft YaHei UI", 15, "bold"))
    val.pack(pady=(0, 10))
    f.value = val          # 供外部更新数值
    return f


class WatchApp:
    def __init__(self, root, core):
        self.root = root
        self.core = core
        root.title(APP_TITLE)
        root.configure(bg=BG)
        # 默认窗口尺寸（按当前实际窗口抓取：976x884）
        root.geometry("1420x1100")
        root.minsize(760, 680)
        self._last_chart_key = None
        self._build()
        self._tick()

    # ---------------- 构建 ----------------
    def _build(self):
        root = self.root

        # 顶栏
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(head, text="🧭 Codex 5h 限额看门狗", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 22, "bold")).pack(side="left")
        self.status_pill = tk.Label(head, text="连接中…", bg=BLUE, fg="#0f172a",
                                    font=("Microsoft YaHei UI", 12, "bold"), padx=14, pady=4)
        self.status_pill.pack(side="right", padx=(8, 0))
        self.plan_pill = tk.Label(head, text="—", bg=PURPLE, fg="#0f172a",
                                  font=("Microsoft YaHei UI", 12, "bold"), padx=14, pady=4)
        self.plan_pill.pack(side="right")
        tk.Label(head, text="本地数据 · 自动续跑", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 11)).pack(side="right", padx=10)

        # KPI 卡
        cards = tk.Frame(root, bg=BG)
        cards.pack(fill="x", padx=10, pady=6)
        for i in range(6):
            cards.grid_columnconfigure(i, weight=1, uniform="kpi")
        self.kpi = {}
        self.kpi["bucket"] = _card(cards, "额度桶", "—", TEAL, 0, 0)
        self.kpi["use5h"] = _card(cards, "5h 用量", "—", BLUE, 0, 1)
        self.kpi["usewk"] = _card(cards, "周用量", "—", PURPLE, 0, 2)
        self.kpi["last_hit"] = _card(cards, "最近到限", "—", RED, 0, 3)
        self.kpi["reset"] = _card(cards, "恢复时间", "—", AMBER, 0, 4)
        self.kpi["proc"] = _card(cards, "Codex 进程", "—", GREEN, 0, 5)

        # 中部：倒计时 + 图表
        mid = tk.Frame(root, bg=BG)
        mid.pack(fill="both", expand=True, padx=10, pady=6)
        mid.grid_columnconfigure(0, weight=2)
        mid.grid_columnconfigure(1, weight=3)
        mid.grid_rowconfigure(0, weight=1)

        left = tk.Frame(mid, bg=CARD_BG, highlightbackground=AMBER, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        tk.Label(left, text="⏳ 恢复倒计时", bg=CARD_BG, fg=AMBER,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(16, 4))
        self.countdown_text = tk.Label(left, text="额度正常", bg=CARD_BG, fg=GREEN,
                                       font=("Microsoft YaHei UI", 34, "bold"))
        self.countdown_text.pack(pady=(6, 2))
        self.countdown_sub = tk.Label(left, text="", bg=CARD_BG, fg=MUTED,
                                      font=("Microsoft YaHei UI", 11))
        self.countdown_sub.pack(pady=(0, 6))
        self.progress = tk.Canvas(left, height=16, bg="#0b1220", highlightthickness=0)
        self.progress.pack(fill="x", padx=20, pady=(4, 16))
        self.hit_msg = tk.Label(left, text="", bg=CARD_BG, fg=MUTED, wraplength=340,
                                font=("Microsoft YaHei UI", 10), justify="left")
        self.hit_msg.pack(fill="x", padx=14, pady=(0, 6))

        # 实时额度监测区（参考 unsnooze usage：5h / 周窗口用量条 + 消耗速率 + ETA）
        live = tk.Frame(left, bg="#0e1526", highlightbackground="#1f2b47", highlightthickness=1)
        live.pack(fill="x", padx=14, pady=(2, 12))
        tk.Label(live, text="📡 实时额度（unsnooze 式监测）", bg="#0e1526", fg=CYAN,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        row5 = tk.Frame(live, bg="#0e1526")
        row5.pack(fill="x", padx=10, pady=(2, 2))
        tk.Label(row5, text="5h 窗口", bg="#0e1526", fg="#9fe8c8", width=7, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.usage_bar5 = tk.Canvas(row5, height=16, bg="#0b1220", highlightthickness=0)
        self.usage_bar5.pack(side="left", fill="x", expand=True, padx=(4, 6))
        self.use5h_lbl = tk.Label(row5, text="—", bg="#0e1526", fg=BLUE,
                                  font=("Microsoft YaHei UI", 13, "bold"), width=7, anchor="e")
        self.use5h_lbl.pack(side="left")
        roww = tk.Frame(live, bg="#0e1526")
        roww.pack(fill="x", padx=10, pady=(2, 2))
        tk.Label(roww, text="周窗口", bg="#0e1526", fg="#c084fc", width=7, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.usage_barw = tk.Canvas(roww, height=16, bg="#0b1220", highlightthickness=0)
        self.usage_barw.pack(side="left", fill="x", expand=True, padx=(4, 6))
        self.usewk_lbl = tk.Label(roww, text="—", bg="#0e1526", fg=PURPLE,
                                  font=("Microsoft YaHei UI", 13, "bold"), width=7, anchor="e")
        self.usewk_lbl.pack(side="left")
        self.burn_text = tk.Label(live, text="消耗速率 — · 预测触顶 —", bg="#0e1526", fg=MUTED,
                                  font=("Microsoft YaHei UI", 10), justify="left", anchor="w")
        self.burn_text.pack(fill="x", padx=10, pady=(2, 0))
        self.snap_age = tk.Label(live, text="额度快照 —", bg="#0e1526", fg=MUTED,
                                 font=("Microsoft YaHei UI", 10), anchor="w")
        self.snap_age.pack(fill="x", padx=10, pady=(0, 8))

        right = tk.Frame(mid, bg=CARD_BG, highlightbackground=BLUE, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        tk.Label(right, text="📊 滚动 5h 用量（每小时 token）", bg=CARD_BG, fg=BLUE,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(14, 4))
        self.chart = tk.Canvas(right, bg=CARD_BG, highlightthickness=0, height=170)
        self.chart.pack(fill="both", expand=True, padx=14, pady=(4, 10))

        # 日志 + 按钮
        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="both", expand=True, padx=16, pady=(6, 12))
        tk.Label(bottom, text="事件日志", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")

        self.log_view = tk.Text(bottom, height=9, bg="#0b1220", fg=TEXT, bd=0,
                                font=("Microsoft YaHei UI", 10), wrap="word",
                                selectbackground=BLUE)
        self.log_view.pack(fill="both", expand=True, pady=(4, 8))
        for tag, color in (("INFO", TEAL), ("WARN", AMBER), ("LIMIT", RED),
                           ("RESUME", GREEN), ("ERROR", RED), ("OK", GREEN)):
            self.log_view.tag_configure(tag, foreground=color)

        btns = tk.Frame(bottom, bg=BG)
        btns.pack(fill="x")
        self.target_lbl = tk.Label(btns, text="目标：最近会话", bg=BG, fg=CYAN,
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.target_lbl.pack(side="left", padx=(0, 10))
        self._btn(btns, "⟳ 立即刷新", BLUE, self.core.scan_now).pack(side="left", padx=(0, 8))
        self._btn(btns, "▶ 手动续跑", GREEN,
                  lambda: self._confirm_resume()).pack(side="left", padx=(0, 8))
        # 自动续跑开关：绿色=开，灰色=关
        auto_on = bool(self.core.cfg.get("auto_resume", True))
        self.auto_btn = tk.Button(
            btns, text="⏱ 自动续跑 开" if auto_on else "⏱ 自动续跑 关",
            command=self._toggle_auto,
            bg=GREEN if auto_on else "#4b5563",
            fg="#0f172a" if auto_on else "#e5e7eb",
            activebackground=GREEN if auto_on else "#4b5563",
            activeforeground="#0f172a" if auto_on else "#e5e7eb",
            font=("Microsoft YaHei UI", 11, "bold"),
            relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
        self.auto_btn.pack(side="left", padx=(0, 8))
        self._btn(btns, "🎯 指定会话…", AMBER, self._pick_session).pack(side="left", padx=(0, 8))
        self._btn(btns, "⏹ 结束 Codex 进程", RED,
                  lambda: self._confirm_kill()).pack(side="left", padx=(0, 8))
        self._btn(btns, "⚙ 设置", PURPLE, self._settings).pack(side="left")

    def _toggle_auto(self):
        """切换自动续跑开关：绿=开，灰=关。"""
        c = self.core.cfg
        c["auto_resume"] = not c.get("auto_resume", True)
        if save_config(c):
            self.core.log("OK", f"自动续跑已{'开启' if c['auto_resume'] else '关闭'}")
        on = bool(c["auto_resume"])
        self.auto_btn.configure(
            text="⏱ 自动续跑 开" if on else "⏱ 自动续跑 关",
            bg=GREEN if on else "#4b5563",
            fg="#0f172a" if on else "#e5e7eb",
            activebackground=GREEN if on else "#4b5563",
            activeforeground="#0f172a" if on else "#e5e7eb")

    def _btn(self, parent, text, color, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="#0f172a",
                         activebackground=color, activeforeground="#0f172a",
                         font=("Microsoft YaHei UI", 11, "bold"),
                         relief="flat", bd=0, padx=14, pady=6, cursor="hand2")

    # ---------------- 状态刷新 ----------------
    def _tick(self):
        self._refresh()
        self._drain_logs()
        self.root.after(500, self._tick)

    def _drain_logs(self):
        try:
            while True:
                ts, level, text = self.core.log_q.get_nowait()
                self.log_view.insert("end", f"[{ts}] ", "INFO")
                self.log_view.insert("end", f"[{level}] ", level)
                self.log_view.insert("end", text + "\n")
                self.log_view.see("end")
        except queue.Empty:
            pass

    def _refresh(self):
        c = self.core
        with c.lock:
            snap, hit, pids = c.snap, c.hit, list(c.process_pids)
            status = c.status
            turns = len(c.rolling)
            tokens = sum(t for _, t in c.rolling)
            rolling = list(c.rolling)

        # 状态 pill
        st_map = {
            "ok": ("额度正常", GREEN), "limited": ("已到限", RED),
            "waiting": ("续跑后确认中", AMBER), "resuming": ("续跑中", CYAN),
            "error": ("异常", RED),
        }
        st_text, st_color = st_map.get(status, ("未知", MUTED))
        self.status_pill.configure(text=st_text, bg=st_color)

        # 续跑目标
        if c.cfg.get("resume_target") == "custom" and c.cfg.get("resume_session_id"):
            sid = c.cfg["resume_session_id"]
            self.target_lbl.configure(text=f"目标：指定 {sid[:12]}…")
        else:
            self.target_lbl.configure(text="目标：最近会话")

        # KPI（参考 unsnooze usage：5h / 周窗口用量百分比）
        self.plan_pill.configure(text="Plus" if snap.get("plan") else "—")
        self.kpi["bucket"].value.configure(text=str(c.main_limit_id() or "—"))
        mu = c.main_limits()
        u5 = mu.get("used5h") if mu else None
        uw = mu.get("used_week") if mu else None
        self.kpi["use5h"].value.configure(
            text=f"{u5:.0f}%" if isinstance(u5, (int, float)) else "—")
        self.kpi["usewk"].value.configure(
            text=f"{uw:.0f}%" if isinstance(uw, (int, float)) else "—")
        self.kpi["last_hit"].value.configure(text=(hit["ts"][11:19] + " UTC" if hit else "无"))
        if hit:
            _rt = c.reset_time(hit)
            if _rt:
                if _rt.date() == datetime.now().date():
                    self.kpi["reset"].value.configure(text="今天 " + _rt.strftime("%H:%M:%S"))
                else:
                    self.kpi["reset"].value.configure(text=_rt.strftime("%m-%d %H:%M"))
            else:
                self.kpi["reset"].value.configure(text="—")
        else:
            self.kpi["reset"].value.configure(text="—")
        self.kpi["proc"].value.configure(text=f"{len(pids)} 个" if pids else "未运行")
        self.kpi["proc"].value.configure(fg=GREEN if pids else MUTED)

        # 倒计时
        now = time.time()
        if hit and status in ("limited", "waiting", "resuming", "probing"):
            reset = c.reset_time(hit)
            if status == "probing":
                self.countdown_text.configure(text="探测中", fg=BLUE)
                self.countdown_sub.configure(text="恢复点已到，正在探测额度…")
                self.hit_msg.configure(text=hit["message"][:160])
            elif reset:
                remain = reset.timestamp() - now
                if remain > 0:
                    h, rem = divmod(int(remain), 3600)
                    m, s = divmod(rem, 60)
                    color = AMBER if remain > 1800 else RED
                    self.countdown_text.configure(text=f"{h:02d}:{m:02d}:{s:02d}", fg=color)
                    self.countdown_sub.configure(text="预计恢复后自动续跑" if c.cfg["auto_resume"] else "已到点（自动续跑已关闭）")
                    frac = min(1.0, 1.0 - remain / WINDOW_SECONDS)
                    self._draw_progress(frac, color)
                else:
                    self.countdown_text.configure(text="已到点", fg=GREEN)
                    self.countdown_sub.configure(text="探测额度中…" if c.cfg["auto_resume"] else "已到点（自动续跑已关闭）")
            else:
                self.countdown_text.configure(text="到限", fg=RED)
            self.hit_msg.configure(text=hit["message"][:160])
        else:
            self.countdown_text.configure(text="额度正常", fg=GREEN)
            self.countdown_sub.configure(text="滚动 5h 窗口内未触发限额")
            self.hit_msg.configure(text="")
            self._draw_progress(0, GREEN)

        # 实时额度监测（用量条 / 消耗速率 / ETA 区间 / 快照年龄）
        self._update_live_metrics()

        # 图表
        self._draw_chart(rolling)

    def _update_live_metrics(self):
        c = self.core
        m = c.burn_metrics()
        mu = c.main_limits()
        u5 = mu.get("used5h") if mu else None
        uw = mu.get("used_week") if mu else None

        def _draw(cv, pct):
            cv.delete("all")
            w = cv.winfo_width() or 320
            cv.create_rectangle(2, 2, w - 2, 12, fill="#0b1220", outline="")
            if isinstance(pct, (int, float)):
                pct = max(0.0, min(100.0, float(pct)))
                pc = BLUE if pct < 70 else (AMBER if pct < 90 else RED)
                cv.create_rectangle(2, 2, 2 + (w - 4) * (pct / 100.0), 12, fill=pc, outline="")

        _draw(self.usage_bar5, u5)
        _draw(self.usage_barw, uw)
        self.use5h_lbl.configure(
            text=f"{u5:.0f}%" if isinstance(u5, (int, float)) else "—",
            fg=GREEN if (isinstance(u5, (int, float)) and u5 >= 85) else BLUE)
        self.usewk_lbl.configure(
            text=f"{uw:.0f}%" if isinstance(uw, (int, float)) else "—",
            fg=GREEN if (isinstance(uw, (int, float)) and uw >= 85) else PURPLE)

        rate = m.get("per_min") if m else None
        if rate and rate > 0:
            rate_h = rate * 60
            rate_s = f"{rate_h:.2f}%/小时"
            eta = c.eta_band(m)
            if eta:
                lo, hi = eta
                def _fmt(mins):
                    hh = int(mins // 60); mm = int(mins % 60)
                    return f"{hh}时{mm:02d}分" if hh else f"{mm}分"
                eta_s = f"预测触顶 {_fmt(lo)} ~ {_fmt(hi)}（估算）"
            else:
                eta_s = "预测触顶 —（数据不足）"
            self.burn_text.configure(text=f"消耗速率 {rate_s} · {eta_s}", fg="#9fe8c8")
        else:
            self.burn_text.configure(text="消耗速率 — · 预测触顶 —（数据不足，持续运行后自动估算）",
                                     fg=MUTED)
        age = m.get("age_s") if m else None
        if age is None:
            self.snap_age.configure(text="额度快照 —")
        else:
            a = int(age)
            a_s = (f"{a}秒前更新" if a < 90 else
                   f"{a // 60}分{a % 60}秒前更新" if a < 5400 else
                   f"{a // 3600}小时前更新")
            color = GREEN if a < 120 else (AMBER if a < 1800 else RED)
            self.snap_age.configure(text=f"额度快照 {a_s}（tail 增量实时监听）", fg=color)

    def _draw_progress(self, frac, color):
        self.progress.delete("all")
        w = self.progress.winfo_width() or 400
        self.progress.create_rectangle(2, 2, w - 2, 14, fill="#0b1220", outline="")
        self.progress.create_rectangle(2, 2, 2 + (w - 4) * min(1.0, max(0.0, frac)), 14,
                                       fill=color, outline="")

    def _draw_chart(self, rolling):
        key = (len(rolling), self.core.rolling_max)
        if key == self._last_chart_key:
            return
        self._last_chart_key = key
        cv = self.chart
        cv.delete("all")
        w = cv.winfo_width() or 600
        h = cv.winfo_height() or 170
        buckets = [0.0] * 5
        now = time.time()
        for ts, total in rolling:
            age = now - ts
            idx = min(4, max(0, int(age // 3600)))
            buckets[idx] += total
        mx = max(buckets) or 1.0
        colors = [TEAL, CYAN, BLUE, PURPLE, PINK]
        bw = (w - 40) / 5
        for i, v in enumerate(buckets):
            bh = max(6, (v / mx) * (h - 46))
            x0 = 20 + i * bw
            cv.create_rectangle(x0, h - 30 - bh, x0 + bw - 8, h - 30,
                                fill=colors[i], outline="")
            cv.create_text(x0 + (bw - 8) / 2, h - 18, text=f"{i + 1}h前",
                           fill=MUTED, font=("Microsoft YaHei UI", 9))
            cv.create_text(x0 + (bw - 8) / 2, h - 34 - bh - 6,
                           text=fmt_tokens(v), fill=colors[i],
                           font=("Microsoft YaHei UI", 9, "bold"))

    # ---------------- 交互 ----------------
    def _confirm_resume(self):
        sid = ""
        if self.core.cfg.get("resume_target") == "custom":
            sid = self.core.cfg.get("resume_session_id") or ""
        desc = f"指定会话 {sid[:12]}…" if sid else "最近会话"
        if not self._ask("手动续跑", f"现在按当前目标（{desc}）执行 codex exec resume 吗？"):
            return
        self.core.request_resume()

    def _pick_session(self):
        """弹窗：列出最近的 Codex 对话，选择后设为续跑目标。"""
        top = tk.Toplevel(self.root)
        top.title("指定续跑对话")
        top.configure(bg=BG)
        top.geometry("700x500")
        top.transient(self.root)

        tk.Label(top, text="最近的 Codex 对话（倒序，前 30 个；双击即可设为续跑目标）",
                 bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(top, text="名称 = 你在 Codex 里给对话起的名字 · 没命名的显示第一条用户消息",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=14)

        frm = tk.Frame(top, bg=BG)
        frm.pack(fill="both", expand=True, padx=14, pady=8)
        lb = tk.Listbox(frm, bg="#0b1220", fg=TEXT, bd=0, selectbackground=BLUE,
                        selectforeground="#0f172a", highlightthickness=0,
                        font=("Microsoft YaHei UI", 10), height=16)
        sb = tk.Scrollbar(frm, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        status = tk.Label(top, text="正在读取聊天记录…", bg=BG, fg=AMBER,
                          font=("Microsoft YaHei UI", 10))
        status.pack(anchor="w", padx=14)

        entry_frm = tk.Frame(top, bg=BG)
        entry_frm.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(entry_frm, text="或粘贴会话 ID：", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(side="left")
        e = tk.Entry(entry_frm, bg="#0b1220", fg=TEXT, insertbackground=TEXT, width=44)
        e.pack(side="left", padx=8)

        items = []

        def apply_sid(sid, label):
            c = self.core.cfg
            c["resume_target"] = "custom"
            c["resume_session_id"] = sid
            if save_config(c):
                self.core.log("OK", f"续跑目标已设为指定会话 {label}")
            top.destroy()

        def apply_selected():
            sel = lb.curselection()
            if not sel or not items:
                return
            it = items[sel[0]]
            apply_sid(it["id"], it["id"][:12])

        def apply_typed():
            sid = e.get().strip()
            if sid:
                apply_sid(sid, sid[:12])

        def back_to_last():
            c = self.core.cfg
            c["resume_target"] = "last"
            c["resume_session_id"] = ""
            if save_config(c):
                self.core.log("OK", "续跑目标已恢复为最近会话")
            top.destroy()

        bf = tk.Frame(top, bg=BG)
        bf.pack(fill="x", padx=14, pady=(0, 12))
        self._btn(bf, "✓ 续跑此对话", GREEN, apply_selected).pack(side="left", padx=(0, 8))
        self._btn(bf, "↺ 恢复默认（最近会话）", PURPLE, back_to_last).pack(side="left", padx=(0, 8))
        self._btn(bf, "✕ 关闭", MUTED, top.destroy).pack(side="left")

        def fill(result):
            nonlocal items
            items = result
            lb.delete(0, "end")
            cur = self.core.cfg.get("resume_session_id") or ""
            sel_idx = None
            for idx, it in enumerate(items):
                ts = f"{it['ts'][5:10].replace('-', '/')} {it['ts'][11:16].replace('-', ':')}"
                shown = it.get("name") or it["summary"] or "（无用户消息）"
                lb.insert("end", f"{ts}   {it['id'][:12]}   {shown}")
                if it["id"] == cur:
                    lb.itemconfigure(idx, foreground=GREEN)   # 当前续跑目标 = 绿色
                    sel_idx = idx
                else:
                    lb.itemconfigure(idx, foreground=TEXT)    # 未选中 = 白色
            if sel_idx is not None:
                lb.selection_set(sel_idx)
                lb.see(sel_idx)
            status.configure(text=f"共 {len(items)} 个会话（绿色=当前续跑目标 · 双击选择，或粘贴会话 ID）")

        def scan():
            try:
                result = scan_sessions(30)
            except Exception as exc:
                result = []
            self.root.after(0, lambda: fill(result))

        threading.Thread(target=scan, daemon=True).start()
        lb.bind("<Double-Button-1>", lambda ev: apply_selected())
        top.bind("<Return>", lambda ev: apply_typed())

    def _confirm_kill(self):
        if not self._ask("结束进程", "确定结束所有检测到的 codex 进程？未保存的工作会丢失。"):
            return
        self.core.request_kill()

    def _ask(self, title, msg):
        import tkinter.messagebox as mb
        return mb.askyesno(title, msg, parent=self.root)

    def _settings(self):
        """设置对话框：轮询间隔 + 自动续跑提示语，同一彩色大窗口。"""
        c = self.core.cfg
        top = tk.Toplevel(self.root)
        top.title("设置")
        top.configure(bg=BG)
        top.geometry("780x720")
        top.minsize(700, 620)
        top.transient(self.root)

        tk.Label(top, text="⚙ 设置", bg=BG, fg=PURPLE,
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=22, pady=(16, 2))
        tk.Label(top, text="保存后立即生效，无需重启看板", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=22)

        # ── 按钮区先钉在底部，保证始终可见 ──
        bf = tk.Frame(top, bg=BG)
        bf.pack(side="bottom", fill="x", padx=22, pady=(0, 18))

        def save():
            try:
                sec = int(spin.get().strip())
            except Exception:
                sec = DEFAULT_POLL
            sec = max(5, min(1800, sec))
            c["poll_interval_sec"] = sec
            try:
                mx = int(maxspin.get().strip())
            except Exception:
                mx = int(c.get("auto_resume_max") or 3)
            c["auto_resume_max"] = max(0, mx)
            c["auto_resume"] = bool(auto_var.get())
            prompt = txt.get("1.0", "end").strip()
            if prompt:
                c["resume_prompt"] = prompt
            if save_config(c):
                self.core.log("OK", f"设置已保存：轮询 {sec}s · 自动续跑 {'开' if c['auto_resume'] else '关'} · 上限 {c['auto_resume_max']} 次")
            top.destroy()

        self._btn(bf, "💾 保存并应用", GREEN, save).pack(side="left", padx=(0, 10))
        self._btn(bf, "取消", MUTED, top.destroy).pack(side="left")

        # ── 轮询间隔 ──
        card1 = tk.Frame(top, bg=CARD_BG, highlightbackground=BLUE, highlightthickness=1)
        card1.pack(fill="x", padx=22, pady=(12, 8))
        tk.Label(card1, text="🔄 轮询间隔", bg=CARD_BG, fg=BLUE,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(card1, text="多久读取一次额度与进程状态", bg=CARD_BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
        row = tk.Frame(card1, bg=CARD_BG)
        row.pack(fill="x", padx=14, pady=(4, 12))
        tk.Label(row, text="每", bg=CARD_BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 11)).pack(side="left")
        spin = tk.Spinbox(row, from_=5, to=1800, increment=5, width=9,
                          bg="#0b1220", fg=TEXT, insertbackground=TEXT,
                          buttonbackground="#2a3140", relief="flat", bd=1,
                          font=("Microsoft YaHei UI", 11, "bold"), justify="center")
        spin.delete(0, "end")
        spin.insert(0, str(c["poll_interval_sec"] if isinstance(c["poll_interval_sec"], int) else DEFAULT_POLL))
        spin.pack(side="left", padx=6)
        tk.Label(row, text="秒", bg=CARD_BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 11)).pack(side="left")
        tk.Label(row, text="范围 5 – 1800 秒（最长 30 分钟一次）", bg=CARD_BG, fg=AMBER,
                 font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(10, 0))

        # ── 自动续跑 ──
        card3 = tk.Frame(top, bg=CARD_BG, highlightbackground=AMBER, highlightthickness=1)
        card3.pack(fill="x", padx=22, pady=(0, 8))
        tk.Label(card3, text="⏱ 自动续跑", bg=CARD_BG, fg=AMBER,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(card3, text="到点后由看门狗自动执行续跑，不用手动点", bg=CARD_BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
        row3 = tk.Frame(card3, bg=CARD_BG)
        row3.pack(fill="x", padx=14, pady=(4, 12))
        auto_var = tk.BooleanVar(value=bool(c["auto_resume"]))
        tk.Checkbutton(row3, text="启用自动续跑", variable=auto_var, bg=CARD_BG, fg=TEXT,
                       activebackground=CARD_BG, activeforeground=GREEN,
                       selectcolor="#0b1220", font=("Microsoft YaHei UI", 11, "bold"),
                       highlightthickness=0).pack(side="left")
        tk.Label(row3, text=" 每个到限周期最多自动", bg=CARD_BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(12, 0))
        maxspin = tk.Spinbox(row3, from_=0, to=20, width=5,
                             bg="#0b1220", fg=TEXT, insertbackground=TEXT,
                             buttonbackground="#2a3140", relief="flat", bd=1,
                             font=("Microsoft YaHei UI", 11, "bold"), justify="center")
        maxspin.delete(0, "end")
        maxspin.insert(0, str(int(c["auto_resume_max"])))
        maxspin.pack(side="left", padx=6)
        tk.Label(row3, text="次（0 = 不限制，失败后持续重试）", bg=CARD_BG, fg=AMBER,
                 font=("Microsoft YaHei UI", 10)).pack(side="left")

        # ── 自动续跑提示语 ──
        card2 = tk.Frame(top, bg=CARD_BG, highlightbackground=GREEN, highlightthickness=1)
        card2.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        tk.Label(card2, text="✍️ 自动续跑提示语", bg=CARD_BG, fg=GREEN,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(card2, text="到点自动执行 codex exec resume 时附带的话 · 可多行换行",
                 bg=CARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
        txt = tk.Text(card2, bg="#0b1220", fg=TEXT, insertbackground=TEXT, bd=0,
                      font=("Microsoft YaHei UI", 11), wrap="word",
                      padx=12, pady=10, selectbackground=BLUE, selectforeground="#0f172a")
        txt.insert("1.0", c["resume_prompt"])
        txt.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        top.grab_set()
        top.focus_set()


def fmt_tokens(v):
    if v >= 1_0000_0000:
        return f"{v / 1_0000_0000:.1f}亿"
    if v >= 1_0000:
        return f"{v / 1_0000:.0f}万"
    return f"{v:.0f}"


# ----------------------------------------------------------------------------
# 会话记录读取（用于"指定对话"：列出最近的 Codex 对话）
# ----------------------------------------------------------------------------
def first_user_text(path, max_lines=20000):
    """从 rollout 文件头部提取第一条真实用户消息文本（对话摘要）。
    兼容两种形态：交互式会话的 item_completed/UserMessage，
    以及 exec 会话的 response_item/message(role=user)；跳过系统注入的 AGENTS.md。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                p = o.get("payload") or {}
                texts = []
                if o.get("type") == "event_msg" and p.get("type") == "item_completed":
                    item = p.get("item") or {}
                    if item.get("type") == "UserMessage":
                        texts = [str(b.get("text")) for b in (item.get("content") or [])
                                 if isinstance(b, dict) and b.get("text")]
                elif (o.get("type") == "response_item" and p.get("type") == "message"
                      and p.get("role") == "user"):
                    texts = [str(b.get("text")) for b in (p.get("content") or [])
                             if isinstance(b, dict) and b.get("text")]
                if not texts:
                    continue
                t = " ".join(" ".join(texts).split())
                if not t:
                    continue
                # 跳过系统注入（AGENTS.md / 指令模板 / 环境上下文 / 页面快照），取真正用户说的话
                if (t.startswith("# AGENTS.md") or t.startswith("<INSTRUCTIONS>")
                        or t.startswith("You are") or t.startswith("<")):
                    continue
                return t[:80]
    except Exception:
        pass
    return ""


def last_agent_summary(path, tail_bytes=512 * 1024):
    """兜底摘要：从文件尾部读最后一条 task_complete 的收尾总结。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read().decode("utf-8", errors="replace")
        for line in reversed(data.splitlines()):
            if '"task_complete"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            p = o.get("payload") or {}
            if p.get("type") == "task_complete":
                t = p.get("last_agent_message") or ""
                t = " ".join(t.split())
                if t:
                    return "→ " + t[:80]
    except Exception:
        pass
    return ""


def load_session_names():
    """读 session_index.jsonl（Codex 保存的用户命名）：id -> thread_name。
    用户在 Codex 里给对话起的名字在这里，比自动摘要更准确。"""
    names = {}
    try:
        with open(str(Path.home() / ".codex" / "session_index.jsonl"), "r",
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("id") and o.get("thread_name"):
                    names[o["id"]] = o["thread_name"]
    except Exception:
        pass
    return names


def read_session_meta(path):
    """读 rollout 首行的 session_meta：根会话 id、是否子代理线程。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                o = json.loads(line)
                if o.get("type") != "session_meta":
                    continue
                pl = o.get("payload") or {}
                return {
                    "session_id": pl.get("session_id"),
                    "is_subagent": bool(pl.get("parent_thread_id"))
                                   or "subagent" in (pl.get("source") or {}),
                }
    except Exception:
        pass
    return None


def scan_sessions(limit=30):
    """扫描 ~/.codex/sessions 下最近的【根对话】（跳过子代理线程），
    按创建时间倒序、按根会话去重，返回 [{ts, id, summary, path}]。"""
    items = []
    names = load_session_names()
    try:
        for p in SESSION_DIR.rglob("rollout-*.jsonl"):
            m = re.match(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-([0-9a-f-]+)\.jsonl$", p.name)
            if not m:
                continue
            ts, sid = m.group(1), m.group(2)
            meta = read_session_meta(p)
            if meta is None or meta.get("is_subagent"):
                continue                      # 子代理线程不是用户对话，跳过
            items.append((ts, meta.get("session_id") or sid, p))
    except Exception:
        return []
    items.sort(key=lambda x: x[0], reverse=True)
    out = []
    seen = set()
    for ts, sid, p in items:
        if sid in seen:
            continue
        seen.add(sid)
        out.append({"ts": ts, "id": sid,
                    "name": names.get(sid, ""),          # 用户命名的名字（可能为空）
                    "summary": first_user_text(p) or last_agent_summary(p),
                    "path": str(p)})
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    cfg = load_config()
    if "--probe" in sys.argv:
        sys.exit(probe(cfg))

    # 单实例保护：只允许一个看门狗运行，避免双重自动续跑/日志混写
    # 注意：Windows 上不要加 SO_REUSEADDR（会允许同一端口重复绑定，导致保护失效）
    try:
        _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock.bind(("127.0.0.1", 27689))
        _lock.listen(1)
    except OSError:
        try:
            _r = tk.Tk(); _r.withdraw()
            tk.messagebox.showwarning("Codex 看门狗", "看门狗已在运行（单实例）。\n请查看已打开的窗口；若需要重启，先关闭旧窗口。")
            _r.destroy()
        except Exception:
            pass
        sys.exit(0)

    core = Core(cfg)
    core.log("OK", f"看板启动 · 轮询间隔 {cfg['poll_interval_sec']}s · 自动续跑 {'开' if cfg['auto_resume'] else '关'} · 次数上限 {cfg['auto_resume_max']}")
    root = tk.Tk()
    root.deiconify()          # 保险：避免偶发启动即最小化/移出屏幕
    root.lift()
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    WatchApp(root, core)
    try:
        root.mainloop()
    finally:
        core.stop()


if __name__ == "__main__":
    main()
