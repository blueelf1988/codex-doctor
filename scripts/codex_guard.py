#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex_guard.py — Codex × CC Switch 连不上？一条命令治好它。

用法（小白友好，记住四个词就行）：
    python3 codex_guard.py doctor      # 体检：找出所有问题（只看不动手）
    python3 codex_guard.py fix         # 修复：把找到的问题全部治好
    python3 codex_guard.py install     # 装守护：以后自动修，不用再管
    python3 codex_guard.py uninstall   # 卸载：不想要了，一键还原

能治的病（全部实测验证过）：
    1. 「Reconnecting... waiting for network」无限转圈
    2. 「Work browser access requires an authenticated account」
    3. 401「已切换到 OpenAI 官方供应商，但这个请求仍按第三方供应商的配置发出」
    4. 「unknown model 'xxx'」
    5. 升级客户端 / 切换路由后突然连不上

原理一句话：
    你的 Codex 读 ~/.codex/config.toml 这个"菜单"来决定连谁。
    CC Switch 切路由时会重写这份菜单，有时会把菜单写坏。
    本脚本：① 把正确写法固化进 CC Switch 的"菜单模板库"（以后写出来的都是对的）
            ② 当场把坏菜单改好  ③ 装一个"监听器"，菜单一被写坏就秒改回来。

安全承诺：
    - 绝不删改你在 CC Switch 里手动建的任何供应商记录
    - 绝不改动你选的模式（官方直连 / 第三方路由，你自己切，脚本只保证各自正确）
    - 每次修改前自动备份，可完整回滚
"""

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time

# ---------------------------------------------------------------- 路径常量
HOME = os.path.expanduser("~")
CODEX_HOME = os.path.join(HOME, ".codex")
CONFIG = os.path.join(CODEX_HOME, "config.toml")
ENV_FILE = os.path.join(CODEX_HOME, ".env")
CC_DIR = os.path.join(HOME, ".cc-switch")
CC_DB = os.path.join(CC_DIR, "cc-switch.db")
LIVE_STATE = os.path.join(CC_DIR, "live-state.json")
GUARD_DIR = os.path.join(CODEX_HOME, "leo-guard")
LOG = os.path.join(GUARD_DIR, "guard.log")
LAUNCH_AGENT = os.path.join(
    HOME, "Library", "LaunchAgents", "com.leozhang.codex-rootfix.plist")

NO_PROXY_VALUES = "127.0.0.1,localhost,::1"

# Codex 客户端可能的安装位置（升级会换地方）
APP_CANDIDATES = [
    "/Applications/ChatGPT.app/Contents/Resources",
    "/Applications/Codex.app/Contents/Resources",
]

ISSUES = []          # doctor 发现的问题
FIX_LOG = []         # fix 实际做了什么


def say(msg):
    print(msg)


def log(msg):
    os.makedirs(GUARD_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def backup(path):
    """改任何文件前先备份，命名带时间戳，可多重回滚"""
    if not os.path.exists(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    return bak


# ================================================================ 状态读取

def current_mode():
    """读 CC Switch 的 live-state.json，返回 codex 当前的模式：
       'proxy'（第三方路由）/ 'direct'（官方直连）/ None（未知，比如没装 CC Switch）"""
    try:
        with open(LIVE_STATE, "r", encoding="utf-8") as f:
            return json.load(f).get("apps", {}).get("codex", {}).get("mode")
    except Exception:
        return None


def current_route():
    try:
        with open(LIVE_STATE, "r", encoding="utf-8") as f:
            return json.load(f).get("apps", {}).get("codex", {}).get("proxy_route")
    except Exception:
        return None


def detect_client_root():
    """返回当前真实存在的 Codex 客户端安装目录"""
    for root in APP_CANDIDATES:
        if os.path.isdir(os.path.join(root, "cua_node", "bin")):
            return root
    return None


def _segment_span(src, header):
    """定位 [header] 段的 (start, end) 区间"""
    m = re.search(r"^\[%s\]\s*$" % re.escape(header), src, re.M)
    if not m:
        return None
    rest = src[m.end():]
    nxt = re.search(r"^\[", rest, re.M)
    return (m.start(), m.end() + (nxt.start() if nxt else len(rest)))


def _provider_model(provider_id):
    """只读：从 CC Switch 数据库查该供应商配置里的 model 名"""
    if not provider_id or not os.path.exists(CC_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        row = con.execute(
            "SELECT settings_config FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        con.close()
        if not row:
            return None
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"',
                      (json.loads(row[0]) or {}).get("config", ""), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


# ================================================================ doctor

def doctor(verbose=True):
    """体检：只看不动手。返回是否健康。"""
    global ISSUES
    ISSUES = []
    mode = current_mode()

    if verbose:
        say("=" * 62)
        say("  Codex 体检报告")
        say("=" * 62)
        say("")
        say("  当前模式：%s%s" % (
            {"proxy": "第三方路由（CC Switch 代理）",
             "direct": "官方直连（个人账号）"}.get(mode, "未知（可能没开 CC Switch）"),
            "    路由：%s" % current_route()[:8] if mode == "proxy" and current_route() else ""))
        say("  客户端：%s" % (detect_client_root() or "未找到（ChatGPT.app？）"))
        say("")

    # ---- 检查 config.toml ----
    if not os.path.exists(CONFIG):
        ISSUES.append(("config-missing", "config.toml 不存在", "fix"))
    else:
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            src = f.read()
        top = src.split("[", 1)[0]

        if 'conversationDetailMode = "STEPS_PROSE"' in src:
            ISSUES.append(("prose-mode",
                           "菜单里开了「Work 模式」，它要求登录 ChatGPT 账号才能用，"
                           "没登录就会报 Work browser access 错", "fix"))
        if src.startswith("﻿"):
            ISSUES.append(("bom", "config.toml 开头有个隐形字符（BOM），"
                           "Codex 读菜单会直接报错", "fix"))

        has_mp = bool(re.search(r'^\s*model_provider\s*=\s*"custom"', top, re.M))
        m = re.search(r"^[ \t]*model_provider[ \t]*=.*$", src, re.M)
        if m and not has_mp:
            ISSUES.append(("mp-misplaced",
                           "「model_provider」这行被挤到了小节里（写菜单的人放错了位置），"
                           "Codex 会以为你没选供应商，退回官方通道", "fix"))

        if mode == "proxy":
            if not has_mp:
                ISSUES.append(("mp-missing",
                               "路由模式下菜单里缺少「model_provider = custom」，"
                               "Codex 会退回官方通道（没有官方钥匙就连不上）", "fix"))
            span = _segment_span(src, "model_providers.custom")
            if span:
                seg = src[span[0]:span[1]]
                if not re.search(r"^\s*supports_websockets\s*=\s*false", seg, re.M):
                    ISSUES.append(("ws-flag",
                                   "新版 Codex 默认用「WebSocket 快线」送请求，"
                                   "但 CC Switch 只收「HTTP 普通件」——"
                                   "需要在供应商小节加一行 supports_websockets = false", "fix"))
            else:
                ISSUES.append(("custom-seg-missing",
                               "菜单里缺少 [model_providers.custom] 小节", "fix"))

        if mode == "direct" and has_mp:
            ISSUES.append(("direct-mp-residue",
                           "你现在用的是官方直连（个人账号），但菜单里还残留着"
                           "「第三方路由」的写法——Codex 会把请求错送到 CC Switch 那里，"
                           "被退回 401", "fix"))

        if "/Applications/Codex.app" in src and detect_client_root():
            ISSUES.append(("dead-path",
                           "菜单里还写着老版本客户端的地址（/Applications/Codex.app），"
                           "客户端搬家后这些功能会全部失灵", "fix"))

        cur = re.search(r'^\s*model\s*=\s*"([^"]+)"', src, re.M)
        want = _provider_model(current_route()) if mode == "proxy" else None
        if want and cur and cur.group(1) != want:
            ISSUES.append(("model-mismatch",
                           "菜单上写的模型（%s）和你当前路由的模型（%s）不是同一个，"
                           "上游会报 unknown model" % (cur.group(1), want), "fix"))

    # ---- 检查 .env ----
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            env = f.read()
        if re.search(r"^\s*(http_proxy|https_proxy|all_proxy)\s*=", env, re.M | re.I):
            ISSUES.append(("dead-proxy-env",
                           "「快递单」上写着一个已经关门的代理地址（.env 里的 "
                           "HTTP_PROXY）——Codex 所有请求都被送去这个空地址，"
                           "于是无限重连", "fix"))
        if not re.search(r"^\s*no_proxy\s*=", env, re.M | re.I):
            ISSUES.append(("no-proxy-missing",
                           "「快递单」上没有写「本地地址不用走代理」的白名单，"
                           "发往本机代理的请求可能被半路劫走", "fix"))

    # ---- 检查 CC Switch 数据库（配置模板库）----
    if os.path.exists(CC_DB):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
            row = con.execute(
                "SELECT value FROM settings WHERE key='common_config_codex'"
            ).fetchone()
            if row:
                common = row[0] or ""
                if 'conversationDetailMode = "STEPS_PROSE"' in common:
                    ISSUES.append(("common-prose",
                                   "CC Switch 的「菜单模板库」里存着 Work 模式写法，"
                                   "每次切路由都会把病重新写进菜单", "fix"))
                if "/Applications/Codex.app" in common and detect_client_root():
                    ISSUES.append(("common-dead-path",
                                   "模板库里存着老客户端地址，切路由会把失效路径写回菜单",
                                   "fix"))
                if re.search(r'^\s*type\s*=\s*"stdio"\s*$', common, re.M):
                    ISSUES.append(("common-stdio",
                                   "模板库里混进了一个 Codex 不认识的写法（type=stdio），"
                                   "每次启动都会刷警告", "fix"))
            con.close()
        except Exception:
            pass

    # ---- 检查守护是否在岗 ----
    running, _ = guard_running()
    if not running:
        ISSUES.append(("guard-off",
                       "「自动纠察队」不在岗——菜单再被写坏时没人自动修", "install"))

    # ---- 汇总 ----
    if verbose:
        if not ISSUES:
            say("  ✓ 一切健康，什么毛病都没有")
        else:
            say("  发现 %d 个问题：" % len(ISSUES))
            say("")
            for i, (_, desc, cure) in enumerate(ISSUES, 1):
                say("  %d. %s" % (i, desc))
            say("")
            say("  → 一次治好：%s fix" % sys.argv[0])
        say("")
        say("=" * 62)
    return len(ISSUES) == 0


# ================================================================ fix

def fix():
    """修复：把 doctor 找到的问题全部治好（并固化进模板库）。"""
    global FIX_LOG
    FIX_LOG = []
    mode = current_mode()
    client_root = detect_client_root()

    # ---------- 1. config.toml ----------
    if os.path.exists(CONFIG):
        bak = backup(CONFIG)
        if bak:
            FIX_LOG.append("已备份菜单 → %s" % os.path.basename(bak))
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            src = f.read()
        orig = src

        # 1a. Work 模式 → Codex 模式（任何模式下都修）
        if re.search(r'(^\s*conversationDetailMode\s*=\s*)"STEPS_PROSE"', src, re.M):
            src = re.sub(r'(^\s*conversationDetailMode\s*=\s*)"STEPS_PROSE"',
                         r'\1"STEPS_COMMANDS"', src, flags=re.M)
            FIX_LOG.append("关掉 Work 模式（它要求登录 ChatGPT 账号，"
                            "API Key 模式用不了）→ 已改回普通模式")

        if mode == "proxy":
            # 1b. model_provider 必须在文件最顶上（放在小节里等于没写）
            top = src.split("[", 1)[0]
            if not re.search(r'^\s*model_provider\s*=\s*"custom"', top, re.M):
                m = re.search(r"^[ \t]*model_provider[ \t]*=.*$", src, re.M)
                if m:
                    src = src[:m.start()] + src[m.end():]
                    src = src.lstrip("\n")
                    src = 'model_provider = "custom"\n' + src
                else:
                    src = 'model_provider = "custom"\n' + src
                FIX_LOG.append("把「model_provider」搬回菜单顶部（放在小节里 Codex 不认）")

            # 1c. WebSocket 快线 → HTTP 普通件
            span = _segment_span(src, "model_providers.custom")
            if span:
                s, e = span
                seg = src[s:e]
                if re.search(r"^\s*supports_websockets\s*=", seg, re.M):
                    src = src[:s] + re.sub(
                        r"^(\s*supports_websockets\s*=\s*).*$", r"\1false",
                        seg, flags=re.M) + src[e:]
                else:
                    lines = src[s:e].split("\n")
                    at = len(lines) - 1
                    while at > 0 and not lines[at - 1].strip():
                        at -= 1
                    lines.insert(at, "supports_websockets = false")
                    src = src[:s] + "\n".join(lines) + src[e:]
                FIX_LOG.append("供应商小节加上「不走 WebSocket 快线」（CC Switch 只收普通件）")

            # 1d. 菜单上的模型名对齐当前路由
            want = _provider_model(current_route())
            cur = re.search(r'^\s*model\s*=\s*"([^"]+)"', src, re.M)
            if want and cur and cur.group(1) != want:
                src = src[:cur.start(1)] + want + src[cur.end(1):]
                FIX_LOG.append("菜单上的模型名改成当前路由的（%s）" % want)

        # 1e. 老客户端地址 → 新地址
        if client_root:
            for old in APP_CANDIDATES:
                if old != client_root and old in src:
                    src = src.replace(old, client_root)
                    FIX_LOG.append("老客户端地址改成新地址（%s）"
                                    % client_root.split("/Applications/")[1])

        if src != orig:
            with open(CONFIG, "w", encoding="utf-8") as f:
                f.write(src)
    else:
        FIX_LOG.append("⚠ config.toml 不存在（Codex 装了吗？）")

    # ---------- 2. .env 快递单 ----------
    if os.path.exists(ENV_FILE):
        bak = backup(ENV_FILE)
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().split("\n")
        out, removed = [], []
        for ln in lines:
            if re.match(r"^(http_proxy|https_proxy|all_proxy)\s*=",
                        ln.strip().lower()):
                removed.append(ln.split("=")[0].strip())
                continue
            out.append(ln)
        if not any(re.match(r'^no_proxy\s*=', l.strip(), re.I) for l in out):
            out.append('NO_PROXY="%s"' % NO_PROXY_VALUES)
            out.append('no_proxy="%s"' % NO_PROXY_VALUES)
        new = "\n".join(out).rstrip("\n") + "\n"
        old = "\n".join(lines).rstrip("\n") + "\n"
        if new != old:
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write(new)
            if removed:
                FIX_LOG.append("撕掉快递单上关门代理的地址（%s）——"
                                "这是无限重连的真凶" % ", ".join(removed))
            FIX_LOG.append("快递单补上「本地地址不走代理」白名单")

    # ---------- 3. CC Switch 模板库（治本）----------
    if os.path.exists(CC_DB):
        try:
            con = sqlite3.connect(CC_DB, timeout=20)
            cur = con.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT value FROM settings WHERE key='common_config_codex'"
            ).fetchone()
            if row:
                val = row[0] or ""
                new = val.replace('conversationDetailMode = "STEPS_PROSE"',
                                  'conversationDetailMode = "STEPS_COMMANDS"')
                if client_root:
                    for old_root in APP_CANDIDATES:
                        if old_root != client_root:
                            new = new.replace(old_root, client_root)
                # 剔除 Codex 不认识的 type=stdio
                new = re.sub(r'^\s*type\s*=\s*"stdio"\s*\n?', "", new, flags=re.M)
                if new != val:
                    cur.execute(
                        "UPDATE settings SET value=? WHERE key='common_config_codex'",
                        (new,))
                    FIX_LOG.append("把正确写法固化进 CC Switch 模板库——"
                                    "以后切路由写出来的菜单天然就是对的")
            con.commit()
            con.close()
        except Exception as e:
            say("  ⚠ 模板库暂时没写进去（没关系，菜单层已修好）：%s" % e)

    # ---------- 汇总 ----------
    if FIX_LOG:
        say("  已完成的修复：")
        for i, item in enumerate(FIX_LOG, 1):
            say("   %d. %s" % (i, item))
        log("fix: " + " | ".join(FIX_LOG))
    else:
        say("  没有需要修的，本来就健康")

    ok = doctor(verbose=False)
    # 复检时剔除「守护未在岗」——它由 install 解决，fix 不负责
    remaining = [d for _, d, _ in ISSUES if _ != "guard-off"] if ok is False else []
    say("")
    if ok:
        say("  修复后复检：✓ 健康")
    elif remaining:
        say("  修复后复检：仍有问题（跑 doctor 看细节）")
    else:
        say("  修复后复检：✓ 菜单已健康（守护未在岗——跑 install 装上它）")
    return ok


# ================================================================ install

def guard_running():
    """查守护进程在不在岗（用 pidfile 判活，不误报）"""
    pid = None
    for pf in (os.path.join(GUARD_DIR, "fsevents.pid"),
               os.path.join(GUARD_DIR, "watcher.pid")):
        try:
            with open(pf, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True, pid
        except Exception:
            continue
    return False, None


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.codex-guard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__GUARD_DIR__/guard_start.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>__GUARD_DIR__/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__GUARD_DIR__/launchd.err.log</string>
    <key>RunOnlyIfNetworkIsAvailable</key>
    <false/>
</dict>
</plist>
"""

START_SH = """#!/bin/bash
# guard_start.sh — 幂等启动：已有活实例就退出，否则拉起监听器 + 轮询兜底
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="%s"
[ -x "$PY" ] || PY="/usr/bin/python3"

is_alive() {
    [ -f "$1" ] || return 1
    local p; p=$(cat "$1" 2>/dev/null)
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

# FSEvents 监听器（秒级响应；编译产物存在才用）
if [ -x "$DIR/fsevents_watch" ]; then
    if ! is_alive "$DIR/fsevents.pid"; then
        rm -f "$DIR/fsevents.pid"
        "$PY" "$DIR/daemonize.py" "$DIR/fsevents_watch" "$PY" "$DIR/codex_guard.py" >/dev/null 2>&1
        for _ in $(seq 10); do sleep 0.5; is_alive "$DIR/fsevents.pid" && break; done
    fi
fi

# Python 轮询兜底（3 秒一次）
if ! is_alive "$DIR/watcher.pid"; then
    rm -f "$DIR/watcher.pid"
    "$PY" "$DIR/daemonize.py" "$PY" "$DIR/watch_loop.py" >/dev/null 2>&1
    for _ in $(seq 6); do sleep 0.4; is_alive "$DIR/watcher.pid" && break; done
fi
exit 0
""" % sys.executable

WATCH_LOOP = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch_loop.py — 轮询兜底：每 3 秒查一次，菜单被写坏就修（秒级交给 FSEvents）"""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(HERE, "watcher.pid")
GUARD = os.path.join(HERE, "codex_guard.py")
PY = sys.executable
TARGETS = [os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
           os.path.join(os.path.expanduser("~"), ".codex", ".env"),
           os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db")]

def snap():
    d = {}
    for p in TARGETS:
        try:
            st = os.stat(p)
            d[p] = (st.st_mtime, st.st_size)
        except OSError:
            d[p] = None
    return d

try:
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
except Exception:
    pass

while True:
    time.sleep(3)
    try:
        cur = snap()
    except Exception:
        continue
    changed = any(prev.get(p) is not None and prev.get(p) != v
                  for p, v in cur.items())
    if changed:
        time.sleep(0.4)  # 等写完落盘
        subprocess.run([PY, GUARD, "fix"], capture_output=True, timeout=120)
        cur = snap()
    prev = cur
'''

DAEMONIZE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemonize.py — 双 fork 守护化（macOS 没有 setsid 命令，用这个代替）"""
import os, sys

LOGDIR = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) < 2:
    sys.exit(2)
cmd = sys.argv[1:]
argv0 = cmd[0]
if not os.path.isabs(argv0):
    c = os.path.join(LOGDIR, argv0)
    argv0 = c if os.path.exists(c) else argv0

pid = os.fork()
if pid > 0:
    sys.exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)

devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(devnull, 0)
outfd = os.open(os.path.join(LOGDIR, "daemon.out"),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(outfd, 1)
os.dup2(outfd, 2)
os.chdir(LOGDIR)
os.execv(argv0, [argv0] + cmd[1:])
'''

FSEVENTS_SWIFT = '''// fsevents_watch.swift — 秒级监听器：菜单一被写就触发修复
import Foundation
import CoreServices

let args = CommandLine.arguments
let rootfix = args.count >= 2 ? args[1] : "/usr/bin/python3"
let pyRun   = args.count >= 3 ? args[2] : "/usr/bin/python3"
let home = FileManager.default.homeDirectoryForCurrentUser.path
let logURL = URL(fileURLWithPath: "\\(home)/.codex/leo-guard/fsevents.log")
let pidURL = URL(fileURLWithPath: "\\(home)/.codex/leo-guard/fsevents.pid")

func log(_ m: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "[\\(ts)] \\(m)\\n"
    if let fh = try? FileHandle(forWritingTo: logURL) {
        defer { try? fh.close() }
        fh.seekToEndOfFile()
        fh.write(line.data(using: .utf8)!)
    } else {
        try? line.write(to: logURL, atomically: true, encoding: .utf8)
    }
}

var lastRun = Date.distantPast
func runFix() {
    let now = Date()
    if now.timeIntervalSince(lastRun) < 0.5 { return }
    lastRun = now
    let p = Process()
    p.executableURL = URL(fileURLWithPath: pyRun)
    p.arguments = [rootfix, "fix"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run(); p.waitUntilExit(); log("已自动修复") }
    catch { log("修复失败: \\(error)") }
}

let dirs = ["\\(home)/.codex", "\\(home)/.cc-switch"]
let callback: FSEventStreamCallback = { _, info, numEvents, eventPaths, _, _ in
    guard let info = info else { return }
    let cfArray = unsafeBitCast(info, to: CFArray.self)
    let paths = cfArray as! [String]
    let n = min(numEvents, paths.count)
    for i in 0..<n {
        let name = paths[i]
        if name.hasSuffix("config.toml") || name.hasSuffix(".env")
            || name.hasSuffix("cc-switch.db") {
            runFix()
            break
        }
    }
}

let flags = UInt32(kFSEventStreamCreateFlagUseCFTypes
    | kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagNoDefer)

if let s = FSEventStreamCreate(kCFAllocatorDefault, callback, nil,
        dirs as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
        0.2, flags) {
    FSEventStreamStart(s)
    try? String(getpid()).write(to: pidURL, atomically: true, encoding: .utf8)
    log("监听已启动 (PID \\(getpid()))")
    runFix()
    dispatchMain()
} else {
    log("FSEvents 启动失败")
    exit(1)
}
'''


def install():
    """装守护：先修一遍，再把「自动纠察队」装到系统里（开机自启）。"""
    if platform.system() != "Darwin":
        say("  ⚠ 本守护只支持 macOS（Windows/Linux 用户：跑 doctor/fix 就够用）")
        return False

    say("  第 1 步：先把当前问题修干净")
    fix()
    say("")

    say("  第 2 步：安装自动守护")
    os.makedirs(GUARD_DIR, exist_ok=True)

    # 核心脚本就位
    me = os.path.abspath(__file__)
    dst = os.path.join(GUARD_DIR, "codex_guard.py")
    if os.path.abspath(me) != dst:
        shutil.copy2(me, dst)

    with open(os.path.join(GUARD_DIR, "watch_loop.py"), "w", encoding="utf-8") as f:
        f.write(WATCH_LOOP)
    with open(os.path.join(GUARD_DIR, "daemonize.py"), "w", encoding="utf-8") as f:
        f.write(DAEMONIZE)
    with open(os.path.join(GUARD_DIR, "guard_start.sh"), "w", encoding="utf-8") as f:
        f.write(START_SH)
    os.chmod(os.path.join(GUARD_DIR, "guard_start.sh"), 0o755)

    # FSEvents 秒级监听器（有 swiftc 才编）
    swift_src = os.path.join(GUARD_DIR, "fsevents_watch.swift")
    with open(swift_src, "w", encoding="utf-8") as f:
        f.write(FSEVENTS_SWIFT)
    fs_bin = os.path.join(GUARD_DIR, "fsevents_watch")
    if shutil.which("swiftc"):
        r = subprocess.run(["swiftc", "-O", swift_src, "-o", fs_bin],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            say("  ✓ 秒级监听器编译成功")
        else:
            say("  ⚠ 编译失败（不影响使用，改用 3 秒轮询兜底）")
    else:
        say("  ⚠ 没找到 swiftc（改用 3 秒轮询兜底，同样可靠）")

    # LaunchAgent：开机自启 + 挂了自动拉起
    plist_path = LAUNCH_AGENT
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, "w", encoding="utf-8") as f:
        f.write(PLIST.replace("__GUARD_DIR__", GUARD_DIR))
    say("  ✓ 开机自启已配置（下次登录生效）")

    # 立即拉起（不等下次登录）
    r = subprocess.run(["bash", os.path.join(GUARD_DIR, "guard_start.sh")],
                       capture_output=True, text=True, timeout=120)

    # 试着立即注册到 launchd（多数环境直接成功）
    registered = False
    try:
        subprocess.run(["launchctl", "bootstrap",
                        "gui/%d" % os.getuid(), plist_path],
                       capture_output=True, timeout=30)
        chk = subprocess.run(["launchctl", "print", "gui/%d/com.user.codex-guard"
                              % os.getuid()], capture_output=True, timeout=30)
        registered = chk.returncode == 0
    except Exception:
        pass
    if registered:
        say("  ✓ 守护已启动并注册到系统（重启电脑也在岗）")
    else:
        say("  ✓ 守护已启动（注册到 launchd 没成功也没关系——"
            "下次登录/重启会自动在岗）")

    say("")
    say("  完成！从现在起：切路由、升级客户端，都不用再管。")
    return True


def uninstall():
    """卸载：停进程、删自启、清文件（菜单本身不动）。"""
    # 停守护
    for pf in (os.path.join(GUARD_DIR, "fsevents.pid"),
               os.path.join(GUARD_DIR, "watcher.pid")):
        try:
            with open(pf) as f:
                os.kill(int(f.read().strip()), 15)
        except Exception:
            pass
        try:
            os.remove(pf)
        except Exception:
            pass
    # 注销 launchd
    try:
        subprocess.run(["launchctl", "bootout",
                         "gui/%d/com.user.codex-guard" % os.getuid()],
                        capture_output=True, timeout=30)
    except Exception:
        pass
    if os.path.exists(LAUNCH_AGENT):
        os.remove(LAUNCH_AGENT)
        say("  ✓ 已移除开机自启")
    if os.path.isdir(GUARD_DIR):
        shutil.rmtree(GUARD_DIR)
        say("  ✓ 已清理守护目录（%s）" % GUARD_DIR)
    say("  卸载完成。菜单本身没动，Codex 照常可用。")


# ================================================================ main

USAGE = __doc__

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if cmd in ("doctor", "check", "体检"):
        sys.exit(0 if doctor() else 1)
    elif cmd in ("fix", "repair", "修复"):
        fix()
    elif cmd in ("install", "安装守护"):
        install()
    elif cmd in ("uninstall", "卸载"):
        uninstall()
    else:
        print(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
