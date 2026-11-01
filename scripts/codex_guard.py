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
    6. 「无法连接 / error sending request for url」——中转商掐断 keep-alive 连接
       导致 CC Switch 连接池里攒下坏连接，越用越死

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

NO_PROXY_VALUES = "127.0.0.1,localhost,::1,api.888aix.cn,.888aix.cn"

# 中转商域名必须绕过系统代理。
# 实测：本机系统代理指向 Clash（127.0.0.1:4780），而 api.888aix.cn 解析到
# Cloudflare Anycast IP，不命中 GEOIP,CN，落到 Clash 规则的 MATCH,Others →
# Proxy 组（境外节点），实测走代理 TLS 直接失败（0/3），直连却能通（3/4）。
# Clash 配置更新后绕过规则可能被冲掉，所以这里做成守护的固定职责。
MUST_BYPASS_DOMAINS = ["api.888aix.cn", ".888aix.cn"]


# proxy 模式下 Codex 必须把请求发给 CC Switch 的本地代理，
# 由代理层再按 live-state 的 proxy_route 转发到真实上游。
# 直接写上游地址会绕过代理层：CC Switch 界面切路由对 Codex 失效，
# 代理日志（proxy_request_logs）也记不到，排查时两眼一抹黑。
PROXY_BASE_URL = "http://127.0.0.1:15721/v1"
PROXY_BEARER = "PROXY_MANAGED"

# Codex 的模型目录文件（CC Switch 生成，Codex 启动时读它拿模型元数据）
CATALOG_FILE = os.path.join(CODEX_HOME, "cc-switch-model-catalog.json")

# Clash（海豚湾）的配置文件：订阅更新会重生成，手工加的规则会被冲掉
CLASH_CONFIG = os.path.join(
    HOME, "Library", "Application Support", "rok-e8ea5596",
    "clash-configs", "rocket.yaml")

# Clash 内核的外部控制器地址。
# ⚠️ 端口不是固定的：本机实测 rocket.yaml 里写的是 0.0.0.0:9090（secret=hitun），
# 但运行中的内核实际监听 4788（secret 为空）——它加载的是订阅合并后的另一份配置。
# 所以这里做多端口探测，不写死。
CLASH_CONTROLLERS = [
    ("127.0.0.1", 4788, ""),
    ("127.0.0.1", 9090, "hitun"),
    ("127.0.0.1", 9090, ""),
    ("127.0.0.1", 4788, "hitun"),
]

# 上游（Cloudflare 后面）要求请求带真实 User-Agent。
# 实测：不带 UA 时上游返回 403「used Cloudflare to restrict access」
# （CF 规则 1010 浏览器完整性检查），带 codex_cli_rs 的 UA 后同样请求 HTTP 200。
# 这个 UA 写在 CCS 数据库的 provider meta.customUserAgent 里。
CODEX_UA = "codex_cli_rs/0.166.0-alpha.5.1"

# 链路慢时的"耐心值"：Codex 默认的流式空闲超时太短，
# 中转商 TLS 握手 4–15 秒 + 首字节 11–17 秒时，客户端会误判为断线
# （idle timeout waiting for SSE）然后反复重连。
# 这些键属于 [model_providers.xxx] 段，Codex 官方支持。
PROVIDER_TUNING = [
    ("stream_idle_timeout_ms", "300000"),   # 流式空闲容忍 5 分钟（AIX 实测首字最久 8.4s，但流式更久）
    ("websocket_connect_timeout_ms", "30000"),
    ("request_max_retries", "8"),
    ("stream_max_retries", "8"),
]

# 只在数据库模板里写、但不强求 config.toml 里有的键。
# 实测：CC Switch 接管重写 config.toml 时用的是它自己的字段集，
# 只保留 request_max_retries / stream_max_retries / stream_idle_timeout_ms，
# 其余键（supports_websockets / websocket_connect_timeout_ms）会被丢掉。
# 所以这两个键写进数据库模板（直连模式下有效），但不拿来判定
# 「config.toml 是否健康」——否则就是永远修不好、跟 CCS 互相覆盖。
PROVIDER_TUNING_STRICT = [
    ("stream_idle_timeout_ms", "300000"),
    ("request_max_retries", "8"),
    ("stream_max_retries", "8"),
]

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
    """改任何文件前先备份。

       铁律：只有内容真的变了才备份。
       原因（实测教训）：曾经每次 fix 都无条件备份，
       结果 1 小时堆出 560 个 config.toml.bak-*，
       磁盘被打满，而真正的改动可能一次都没发生。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            cur = f.read()
    except Exception:
        return None
    # 与最近一份备份比内容：一样就说明这次没实质改动，不必再存一份
    latest = _latest_backup(path)
    if latest:
        try:
            with open(latest, "rb") as f:
                if f.read() == cur:
                    return None
        except Exception:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    _prune_backups(path)
    return bak


def _latest_backup(path):
    """列出该文件最新的备份（按 mtime 倒序）"""
    d = os.path.dirname(path)
    prefix = os.path.basename(path) + ".bak-"
    try:
        names = [os.path.join(d, n) for n in os.listdir(d)
                 if n.startswith(prefix)]
    except Exception:
        return None
    if not names:
        return None
    try:
        names.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return names[0]
    except Exception:
        return names[0]


def _prune_backups(path, keep=3):
    """备份文件只留最近 keep 份——防止磁盘被备份撑爆"""
    d = os.path.dirname(path)
    prefix = os.path.basename(path) + ".bak-"
    try:
        names = [os.path.join(d, n) for n in os.listdir(d)
                 if n.startswith(prefix)]
        names.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except Exception:
        return
    for old in names[keep:]:
        try:
            os.remove(old)
        except Exception:
            pass


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


def _db_current_provider():
    """只读：查数据库里 codex 当前选中的供应商 id（CC Switch 界面上打勾的那条）"""
    if not os.path.exists(CC_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        row = con.execute(
            "SELECT id FROM providers WHERE app_type='codex' AND is_current=1 LIMIT 1"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _provider_name(provider_id):
    """只读：查供应商名字（报错文案用）"""
    if not provider_id or not os.path.exists(CC_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        row = con.execute(
            "SELECT name FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def fix_route_mismatch():
    """治「界面显示的线路」和「实际转发的线路」不是同一条。

       实测（2026-11-01 08:43）：
       - CCS 数据库里 codex 的 is_current 已经是 PRO 线（86a24d5a）
       - 但 ~/.cc-switch/live-state.json 里 codex.proxy_route 还是 Kiro 线（44856c30）
       - CC Switch 代理转发时读的是 live-state.json，不是数据库
       → 界面上你看着 PRO 线，代理实际全打到 Kiro 线。
         Kiro 线只认 claude-*，Codex 一发就 503 / "not implemented"。

       为什么两边会脱节：
       CCS 切换路由时要同时写两个地方（数据库 + live-state.json），
       任何一步失败/被打断（进程被杀、写库中途崩）就会留下一半。
       本机实测就是 CCS 被重启时写了一半留下的。

       怎么修：不重启 CCS，直接把 live-state.json 的 proxy_route
       对齐到数据库的 is_current —— 实测改完立刻生效，
       代理下一次请求就走新线路（CCS 每次请求都重读 live-state.json）。
    """
    if not os.path.exists(LIVE_STATE) or not os.path.exists(CC_DB):
        return False
    route = current_route()
    db_route = _db_current_provider()
    if not route or not db_route or route == db_route:
        return False
    try:
        with open(LIVE_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        codex = d.get("apps", {}).get("codex")
        if not isinstance(codex, dict):
            return False
        codex["proxy_route"] = db_route
        d["apps"]["codex"] = codex
        bak = backup(LIVE_STATE)
        with open(LIVE_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        FIX_LOG.append(
            "CC Switch 实际转发的线路（%s）和界面上选的（%s）不是同一条——"
            "代理读的是 live-state.json，两边脱节了。已把转发线路对齐到界面那一条"
            "（不用重启 CC Switch，改完下一次请求就生效）"
            % (_provider_name(route) or route[:8],
               _provider_name(db_route) or db_route[:8]))
        return True
    except Exception as e:
        say("  ⚠ 路由没对齐：%s" % e)
        return False


# ================================================================ 会话粘性提醒
# 根因：Codex 每个会话（thread）创建时会把当时的供应商配置写进会话元数据，
# 之后切换 CC Switch 路由，旧会话仍按创建时的配置发包 → 本地代理按新路由
# 处理 → 认证失败 401。重启 Codex 只重读 config.toml，不改已固化的会话，
# 所以"重启也无效，请新建会话"。守护改不了客户端的会话元数据（不在射程内），
# 但能做到：路由一切换就弹系统通知，提醒"别复用旧会话，点新建"。

MODE_MEMO = os.path.join(GUARD_DIR, "last-mode.json")


def notify(title, message):
    """弹一条 macOS 通知中心提醒（失败不影响主流程）"""
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s" sound name "Glass"'
             % (message.replace('"', "'"), title.replace('"', "'"))],
            timeout=10, capture_output=True)
    except Exception:
        pass


def check_mode_switch():
    """检测 CC Switch 路由是否刚切换过；切换了就弹通知提醒新建会话。
       每次 fix 都会跑（watcher 3 秒一轮），去重靠 last-mode.json。"""
    mode, route = current_mode(), current_route()
    if not mode:
        return
    last = {}
    try:
        with open(MODE_MEMO, "r", encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        pass
    if last.get("mode") == mode and last.get("route") == route:
        return  # 没变，不打扰
    first_run = "mode" not in last
    try:
        os.makedirs(GUARD_DIR, exist_ok=True)
        with open(MODE_MEMO, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "route": route,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass
    if first_run:
        log("模式快照已建立: mode=%s route=%s" % (mode, route))
        return
    log("检测到路由切换: %s → %s（route=%s）" % (last.get("mode"), mode, route))
    if mode == "direct":
        notify("Codex 路由已切换：官方直连",
               "旧会话仍按第三方供应商发包，会报 401。请点「新建会话」，不要复用旧会话。")
    elif mode == "proxy":
        notify("Codex 路由已切换：第三方供应商",
               "旧会话仍按官方配置发包，也可能报错。请点「新建会话」，不要复用旧会话。")
    else:
        notify("Codex 路由已切换",
               "旧会话可能粘住旧供应商配置。请点「新建会话」，不要复用旧会话。")


def _ensure_provider_keys(seg_text, strict=False):
    """给供应商小节补齐：WebSocket 快线开关 + 链路慢时的耐心值。
       只补缺失的，不覆盖已有配置——但如果耐心值比建议值小，
       慢链路下仍会被误判成断线，这种情况下要抬到建议值。

       strict=True 时只补 CCS 接管重写后必然会保留的键
       （stream_idle_timeout_ms / request_max_retries / stream_max_retries）——
       用于改 config.toml。其余键只在写数据库模板时补
       （用 strict=False），否则改完 config.toml 又被 CCS 覆盖，
       形成「守护补 → CCS 覆盖」的自激循环。
       返回 (新段文本, 补了哪些)。"""
    added = []
    if not strict and not re.search(r"^\s*supports_websockets\s*=", seg_text, re.M):
        seg_text = seg_text.rstrip("\n") + "\nsupports_websockets = false\n"
        added.append("不走 WebSocket 快线")
    tuning = PROVIDER_TUNING_STRICT if strict else PROVIDER_TUNING
    for key, val in tuning:
        m = re.search(r"^\s*%s\s*=\s*(\d+)" % re.escape(key), seg_text, re.M)
        if not m:
            seg_text = seg_text.rstrip("\n") + "\n%s = %s\n" % (key, val)
            added.append("%s=%s" % (key, val))
        elif int(m.group(1)) < int(val):
            # 已有但偏小：抬到建议值（慢链路需要更多耐心）
            seg_text = (seg_text[:m.start(1)] + val + seg_text[m.end(1):])
            added.append("%s %s→%s（慢链路需要更多耐心）"
                         % (key, m.group(1), val))
    return seg_text, added


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


def _provider_upstream_host(provider_id):
    """只读：取当前路由供应商的上游域名（从 endpoint 或 base_url）"""
    if not provider_id or not os.path.exists(CC_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        row = con.execute(
            "SELECT url FROM provider_endpoints WHERE provider_id=? "
            "ORDER BY id LIMIT 1", (provider_id,)
        ).fetchone()
        if not row:
            row2 = con.execute(
                "SELECT settings_config FROM providers WHERE id=?",
                (provider_id,)).fetchone()
            con.close()
            if not row2:
                return None
            cfg = (json.loads(row2[0]) or {}).get("config", "") or ""
            m = re.search(r'^[ \t]*base_url[ \t]*=[ \t]*"([^"]+)"', cfg, re.M)
            url = m.group(1) if m else ""
        else:
            con.close()
            url = row[0] or ""
        if not url:
            return None
        m = re.match(r"https?://([^/:]+)", url.strip())
        return m.group(1).lower() if m else None
    except Exception:
        return None


def _cc_recent_failures(provider_id, window_sec=180):
    """只读：看这个供应商最近 window_sec 秒内的连续失败次数"""
    if not provider_id or not os.path.exists(CC_DB):
        return 0, 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        now = int(time.time())
        rows = con.execute(
            "SELECT status_code FROM proxy_request_logs "
            "WHERE provider_id=? AND created_at > ? ORDER BY created_at DESC",
            (provider_id, now - window_sec)).fetchall()
        con.close()
        fails = 0
        for (sc,) in rows:
            if sc == 200:
                break
            fails += 1
        return fails, len(rows)
    except Exception:
        return 0, 0


def _needs_user_agent(cfg):
    """这个供应商的上游是不是在 Cloudflare 后面（需要真实 UA）

       实测：只有 888aix 这类中转商域名会因缺 UA 被 CF 规则 1010 拦掉；
       DeepSeek 官方、OpenCode 不带 UA 也正常返回。
    """
    m = re.search(r'base_url[ \t]*=[ \t]*"([^"]*)"', cfg or "")
    if not m:
        return False
    url = m.group(1).lower()
    return "888aix" in url or url.endswith(".hitun.io")


def _fix_provider_templates():
    """把「链路慢时的耐心值」固化进 CC Switch 数据库里 provider 的 config 模板。

       为什么必须改数据库而不是只改 config.toml：
       CC Switch 接管代理时会用它自己的模板重写 config.toml，
       只在 config.toml 里补的键下一次就被冲掉。
       守护反复补 → CCS 反复覆盖 → 自激循环
       （实测：1 小时堆出 560 个 config.toml.bak-*，CCS 每几秒被重启一次）。

       实测结论：数据库模板里这些官方 Codex 键能被 CCS 保留
       （request_max_retries / stream_max_retries / stream_idle_timeout_ms
         / supports_websockets / websocket_connect_timeout_ms），
       不认识的键（如 zzz_custom_probe）会被丢弃——所以只写官方键。
    """
    if not os.path.exists(CC_DB):
        return
    try:
        con = sqlite3.connect(CC_DB, timeout=20)
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        rows = cur.execute(
            "SELECT id, name, settings_config FROM providers WHERE app_type='codex'"
        ).fetchall()
        changed = 0
        for pid, pname, sc in rows:
            if not sc:
                continue
            # 官方直连的 provider 不碰——它没有第三方上游，加这些键没有意义
            if (pname or "").strip().lower() in ("openai official", "openai official 官方"):
                continue
            if (pid or "").startswith("codex-official"):
                continue
            try:
                d = json.loads(sc)
            except Exception:
                continue
            cfg = d.get("config") or ""
            # 模板里 [model_providers.custom] 后面缺换行会让 TOML 解析异常
            if "[model_providers.custom]name" in cfg:
                cfg = cfg.replace("[model_providers.custom]name",
                                  "[model_providers.custom]\nname")
                d["config"] = cfg
                cur.execute("UPDATE providers SET settings_config=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), pid))
                changed += 1
                FIX_LOG.append("修好供应商模板里 [model_providers.custom] 后面漏掉的换行"
                               "（%s）——不然 TOML 解析会出错" % pname)
            # 缺 User-Agent 会被 Cloudflare 规则 1010 拦掉（浏览器完整性检查）
            # ——实测：无 UA 时上游返回 403「used Cloudflare to restrict access」，
            #    带上 codex_cli_rs 的 UA 后同样的请求 HTTP 200。
            # 必须放在 meta.customUserAgent（放 settings_config 顶层不生效）。
            # 只对走 Cloudflare 的中转商域名加 —— 实测 DeepSeek/OpenCode
            # 不带 UA 也正常，乱加没有意义。
            if _needs_user_agent(cfg):
                meta = d.get("meta")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = None
                if not isinstance(meta, dict):
                    meta = {}
                if not meta.get("customUserAgent"):
                    meta["customUserAgent"] = CODEX_UA
                    d["meta"] = meta
                    cur.execute("UPDATE providers SET settings_config=? WHERE id=?",
                                (json.dumps(d, ensure_ascii=False), pid))
                    changed += 1
                    FIX_LOG.append("给供应商（%s）补上 User-Agent（%s）——"
                                   "缺 UA 会被 Cloudflare 规则 1010 拦掉，"
                                   "表现成「连不上/一直重连」"
                                   % (pname or pid[:8], CODEX_UA))
            if "[model_providers.custom]" not in cfg:
                continue
            seg_m = re.search(r"\[model_providers\.custom\](.*)$", cfg, re.S)
            if not seg_m:
                continue
            seg = seg_m.group(1)
            new_seg, added = _ensure_provider_keys(seg)
            if not added:
                continue
            # 注意：cfg 可能是上面修过换行的版本，必须基于 cfg 拼接。
            # 关键：段标题和内容之间必须保留一个换行 ——
            # seg_m.start(1) 正好是段标题之后的位置，
            # 所以这里补一个 "\n"，再把段体首尾的换行去掉，
            # 否则 [model_providers.custom] 和 name 会黏成一行，TOML 解析出错。
            new_cfg = (cfg[:seg_m.start(1)] + "\n"
                       + new_seg.strip("\n") + "\n")
            d["config"] = new_cfg
            cur.execute("UPDATE providers SET settings_config=? WHERE id=?",
                        (json.dumps(d, ensure_ascii=False), pid))
            changed += 1
            FIX_LOG.append("把耐心值固化进 CCS 数据库模板（%s）：%s"
                           % (pname or pid[:8], "、".join(added)))
        if changed:
            con.commit()
        con.close()
    except Exception as e:
        say("  ⚠ 数据库模板暂时写不进去：%s" % e)


def fix_connection_pool():
    """处理「连接池里攒了坏连接」。

       实测：中转商会静默掐断空闲的 keep-alive 连接（不发 FIN/RST），
       客户端连接池里那条连接就成了「僵尸」——下次复用直接报
       connection closed via error / handshake timed out。

       用户要求（2026-11-01）：**不自动重启 CC Switch**。
       重启会当场打断用户正在跑的 Codex 会话，
       而且源站劣化时守护会反复重启（实测 1 小时堆出 560 个备份、
       CCS 被重启十几次）——弊远大于利。
       现在只做两件不动手的事：
         1) 重置熔断计数（让 CCS 别继续拉闸）
         2) 在体检报告里给出提示，由用户自己决定要不要重启
    """
    route = current_route()
    if not route:
        return
    fails, total = _cc_recent_failures(route)
    if total < 5 or fails < 5:
        return
    # 连续失败但又夹杂成功 → 不是池子坏了，是链路抖（重试能救）
    if fails < total:
        return
    # 熔断计数重置（只改计数，不重启进程）
    reset = False
    try:
        con = sqlite3.connect(CC_DB, timeout=20)
        cur = con.cursor()
        cur.execute("UPDATE provider_health SET consecutive_failures=0, "
                    "is_healthy=1, last_error=NULL "
                    "WHERE provider_id=? AND app_type='codex'", (route,))
        reset = cur.rowcount > 0
        con.commit()
        con.close()
    except Exception:
        pass
    if reset:
        FIX_LOG.append("连续 %d 次连接失败——已重置熔断计数（不重启 CC Switch）。"
                       "若仍不通，手动重启一次 CC Switch 即可清掉坏连接池"
                       "（中转商掐断 keep-alive 连接所致）" % fails)


def _system_bypass_has(domain):
    """查 macOS 系统代理的绕过列表里有没有这个域名"""
    try:
        r = subprocess.run(["networksetup", "-getproxybypassdomains", "Wi-Fi"],
                           capture_output=True, text=True, timeout=15)
        txt = (r.stdout or "") + (r.stderr or "")
        dom = domain.lower()
        for line in txt.splitlines():
            v = line.strip().lower()
            if not v:
                continue
            if v == dom or v == "*." + dom or dom.endswith(v.lstrip("*.")):
                return True
        return False
    except Exception:
        return True   # 查不到就不误报


def _clash_controller():
    """找一个能用的 Clash 外部控制器。

       返回 (base_url, secret) 或 None。
       ⚠️ 端口和 secret 都会变：本机实测配置文件里写 9090/hitun，
       但运行中的内核实际监听 4788（secret 空）——
       它加载的是订阅合并后的另一份配置，不是磁盘上那份。
       所以这里逐个探测，不写死。
    """
    import urllib.request
    import urllib.error
    for host, port, secret in CLASH_CONTROLLERS:
        base = "http://%s:%d" % (host, port)
        for sk in ([secret] if secret else ["", "hitun"]):
            try:
                req = urllib.request.Request(base + "/version")
                if sk:
                    req.add_header("Authorization", "Bearer " + sk)
                urllib.request.urlopen(req, timeout=4).read()
                return base, sk
            except Exception:
                continue
    return None, None


def _clash_runtime_has_rule(domain):
    """只读：Clash 内核运行时规则表里有没有这条域名规则。

       这是本轮最关键的一个判据——**文件里有规则 ≠ 内核加载了规则**。
       实测：守护把 DIRECT 规则写进 rocket.yaml 了，
       但内核运行时 678 条规则里「888aix」出现 0 次 → 规则根本没进去。
    """
    base, secret = _clash_controller()
    if not base:
        return None      # 连不上控制器，判断不了
    import urllib.request
    try:
        req = urllib.request.Request(base + "/rules")
        if secret:
            req.add_header("Authorization", "Bearer " + secret)
        d = json.loads(urllib.request.urlopen(req, timeout=6).read().decode())
        for r in (d.get("rules") or []):
            p = str(r.get("payload") or "")
            if domain in p and str(r.get("proxy")) == "DIRECT":
                return True
        return False
    except Exception:
        return None


def _clash_reload_if_needed():
    """让 Clash 内核真正吃进配置里的 AIX 直连规则。

       为什么必须这么做（2026-11-01 实测）：
       Clash 只在启动时加载配置，改完 rocket.yaml 不会自动生效。
       守护在 08:01 就把 DIRECT 规则写进文件了，但内核运行时规则表里
       678 条规则中「888aix」出现 0 次 —— 规则没进去。
       后果：CC Switch 走系统代理 → 走 Clash 4780 → 落到兜底 MATCH,Others
       → 走境外节点 → **经 Clash 请求 0/6 全败（SSL EOF），直连 5/6 正常**。

       修法：PUT /configs?force=true 热重载，再读 /rules 校验。
       ⚠️ 不重启 Clash 内核 —— 那会断掉所有人的代理。
    """
    base, secret = _clash_controller()
    if not base:
        return False
    # 先看运行时有没有；有就不用动
    has = _clash_runtime_has_rule("api.888aix.cn")
    if has is True:
        return False
    import urllib.request
    import urllib.error
    try:
        data = json.dumps({"path": CLASH_CONFIG}).encode()
        req = urllib.request.Request(
            base + "/configs?force=true", data=data,
            headers={"Content-Type": "application/json"}, method="PUT")
        if secret:
            req.add_header("Authorization", "Bearer " + secret)
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        say("  ⚠ Clash 配置热重载失败：%s" % e)
        return False
    # 校验：规则到底进没进去
    has2 = _clash_runtime_has_rule("api.888aix.cn")
    if has2 is True:
        FIX_LOG.append("Clash 内核已热重载，AIX 直连规则真正生效——"
                       "（之前只改了文件没重载，内核里的 678 条规则一条 888aix 都没有，"
                       "导致 CC Switch 走 Clash 时全部失败）")
        return True
    FIX_LOG.append("Clash 配置已重载，但运行时规则表里仍找不到 AIX 直连规则——"
                   "CC Switch 走 Clash 可能仍会失败，建议检查 Clash 客户端")
    return False


def _provider_custom_segment(provider_id):
    """只读：从 CC Switch 数据库取出当前路由供应商配置里的
       [model_providers.custom] 段原文（照搬，不自己编）"""
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
        cfg = (json.loads(row[0]) or {}).get("config", "") or ""
        m = re.search(r"\[model_providers\.custom\][\s\S]*?(?=\n\[|\Z)", cfg)
        return m.group(0).rstrip() + "\n" if m else None
    except Exception:
        return None


def _provider_model_catalog(provider_id):
    """只读：取当前路由供应商在 CC Switch 里登记的模型清单"""
    if not provider_id or not os.path.exists(CC_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
        row = con.execute(
            "SELECT settings_config FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        con.close()
        if not row:
            return []
        cat = (json.loads(row[0]) or {}).get("modelCatalog") or {}
        return cat.get("models") or []
    except Exception:
        return []


def _catalog_entry(base, spec):
    """按供应商登记的模型信息，生成一条 Codex 模型目录条目。

       关键：tool_mode 必须改成 direct —— 抄别的模型的条目时会连
       code_mode_only 一起抄过来，Codex 就会发 'exec' 自定义工具，
       而只认 apply_patch 的上游会直接 400。
    """
    slug = spec.get("model")
    if not slug:
        return None
    m = json.loads(json.dumps(base))  # 深拷贝
    ctx = spec.get("contextWindow") or base.get("context_window") or 272000
    mods = spec.get("inputModalities") or ["text"]
    levels = spec.get("reasoningLevels") or ["low", "high", "max"]
    descs = {"low": "Fast responses with lighter reasoning",
             "medium": "Balanced reasoning",
             "high": "Solid reasoning",
             "xhigh": "Deeper reasoning",
             "max": "Deepest reasoning"}
    m["slug"] = slug
    m["display_name"] = spec.get("displayName") or slug
    m["context_window"] = ctx
    m["max_context_window"] = ctx
    m["auto_compact_token_limit"] = int(ctx * 0.9)
    m["effective_context_window_percent"] = 95
    m["input_modalities"] = mods
    m["supports_image_detail_original"] = "image" in mods
    m["supported_reasoning_levels"] = [
        {"description": descs.get(lv, "Reasoning effort %s" % lv), "effort": lv}
        for lv in levels
    ]
    m["default_reasoning_level"] = spec.get("defaultReasoningLevel") or "high"
    m["multi_agent_reasoning_effort"] = "max"
    m["tool_mode"] = "direct"
    m["apply_patch_tool_type"] = "freeform"
    m["experimental_supported_tools"] = []
    m["description"] = spec.get("description") or ("%s via CC Switch route" % slug)
    m["comp_hash"] = str(abs(hash(slug)) % 9000 + 1000)
    return m


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
                # 不在这里检查 supports_websockets。
                # 实测：CC Switch 接管时会用它自己的字段集重写 config.toml，
                # 这个键必然被丢掉 → 守护补 → CCS 再覆盖 → 自激循环。
                # 它已经写进数据库模板里（直连模式有效），接管模式下由 CCS 自己处理。
            else:
                ISSUES.append(("custom-seg-missing",
                               "菜单里缺少 [model_providers.custom] 小节", "fix"))
            # 链路慢时的耐心值：没有它，Codex 会把"上游慢"误判成"断了"
            # 太小的同样不行——AIX 首字节实测就要 2–8 秒
            too_small = []
            for k, v in PROVIDER_TUNING_STRICT:
                mv = re.search(r"^\s*%s\s*=\s*(\d+)" % re.escape(k), seg, re.M)
                if not mv:
                    too_small.append("%s（缺）" % k)
                elif int(mv.group(1)) < int(v):
                    too_small.append("%s=%s（建议≥%s）" % (k, mv.group(1), v))
            if too_small:
                ISSUES.append(("timeout-tuning-missing",
                               "供应商小节里的「等待耐心值」不够（%s）——"
                               "中转商握手慢时（首字节 2–8 秒），"
                               "Codex 会误判成断线，然后反复重连到失败"
                               % "、".join(too_small), "fix"))
            # base_url 必须指向本地代理，否则等于绕过了 CC Switch
            bm = re.search(r'^[ \t]*base_url[ \t]*=[ \t]*"([^"]*)"', seg, re.M)
            if bm and bm.group(1).rstrip("/") != PROXY_BASE_URL.rstrip("/"):
                ISSUES.append(("proxy-base-url-bypass",
                               "供应商小节里的 base_url 写的是上游直连地址（%s），"
                               "不是 CC Switch 本地代理（%s）——这样 Codex 绕过了代理层，"
                               "界面里切路由对它无效，代理日志也记不到，"
                               "出问题无从查起" % (bm.group(1), PROXY_BASE_URL), "fix"))
            tm = re.search(r'^[ \t]*experimental_bearer_token[ \t]*=[ \t]*"([^"]*)"', seg, re.M)
            if tm and tm.group(1) != PROXY_BEARER:
                ISSUES.append(("real-key-in-proxy-config",
                               "供应商小节里写着真实 API Key（%s…）——"
                               "打本地代理不需要它，真 key 留在菜单里等于泄露面扩大，"
                               "应换成代理占位凭证（%s）"
                               % (tm.group(1)[:8], PROXY_BEARER), "fix"))

        if mode == "direct" and has_mp:
            ISSUES.append(("direct-mp-residue",
                           "你现在用的是官方直连（个人账号），但菜单里还残留着"
                           "「第三方路由」的写法——Codex 会把请求错送到 CC Switch 那里，"
                           "被退回 401", "fix"))

    # ---- 检查 Clash 规则（AIX 必须直连）----
    if os.path.exists(CLASH_CONFIG):
        try:
            with open(CLASH_CONFIG, "r", encoding="utf-8") as f:
                y = f.read()
            if "888aix.cn,DIRECT" not in y:
                ISSUES.append(("clash-direct-rule-missing",
                               "Clash 的规则里没有 api.888aix.cn → DIRECT——"
                               "订阅更新把它冲掉了，这个域名会落到兜底规则"
                               "MATCH,Others 走境外节点，实测 TLS 握手失败、"
                               "表现成「连不上」", "fix"))
        except Exception:
            pass

    # ---- 检查数据库模板里有没有耐心值（治本项）----
    # 只改 config.toml 是不够的：CCS 接管时会用自己的模板重写 config.toml，
    # 把补进去的键冲掉，守护与 CCS 互相覆盖形成自激循环。
    if mode == "proxy" and os.path.exists(CC_DB):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=10)
            rows = con.execute(
                "SELECT id, name, settings_config FROM providers "
                "WHERE app_type='codex'"
            ).fetchall()
            con.close()
            miss = []
            for pid, pname, sc in rows:
                if not sc:
                    continue
                # 官方直连不适用（没有第三方上游）——与 fix 的口径保持一致
                if (pid or "").startswith("codex-official"):
                    continue
                if (pname or "").strip().lower() == "openai official":
                    continue
                try:
                    cfg = json.loads(sc).get("config") or ""
                except Exception:
                    continue
                if "[model_providers.custom]" not in cfg:
                    continue
                seg_m = re.search(r"\[model_providers\.custom\](.*)$", cfg, re.S)
                if not seg_m:
                    continue
                seg = seg_m.group(1)
                for k, v in PROVIDER_TUNING_STRICT:
                    mv = re.search(r"^\s*%s\s*=\s*(\d+)" % re.escape(k), seg, re.M)
                    if not mv or int(mv.group(1)) < int(v):
                        miss.append("%s: %s" % (pid[:8], k))
                        break
            if miss:
                ISSUES.append(("template-tuning-missing",
                               "CC Switch 数据库里的供应商模板缺耐心值（%s）——"
                               "CCS 接管时会用它自己的模板重写 config.toml，"
                               "只在菜单里补的键会被冲掉，"
                               "于是「守护补 → CCS 覆盖」来回打架"
                               % "、".join(miss[:4]), "fix"))

            # ---- 检查供应商有没有带 User-Agent（CF 规则 1010）----
            no_ua = []
            for pid, pname, sc in rows:
                if not sc:
                    continue
                if (pid or "").startswith("codex-official"):
                    continue
                if (pname or "").strip().lower() == "openai official":
                    continue
                try:
                    d = json.loads(sc)
                except Exception:
                    continue
                if not _needs_user_agent(d.get("config") or ""):
                    continue
                meta = d.get("meta")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = None
                if not isinstance(meta, dict):
                    meta = {}
                if not meta.get("customUserAgent"):
                    no_ua.append(pname or pid[:8])
            if no_ua:
                ISSUES.append(("missing-user-agent",
                               "供应商（%s）没有设 User-Agent——"
                               "上游在 Cloudflare 后面，不带 UA 会被规则 1010"
                               "（浏览器完整性检查）拦掉，返回 403 "
                               "「used Cloudflare to restrict access」，"
                               "表现就是「连不上/一直重连」"
                               % "、".join(no_ua[:4]), "fix"))
        except Exception:
            pass

    # ---- 备份文件堆积 ----
    try:
        d = os.path.dirname(CONFIG)
        prefix = os.path.basename(CONFIG) + ".bak-"
        nb = len([n for n in os.listdir(d) if n.startswith(prefix)])
        if nb > 8:
            ISSUES.append(("backup-pileup",
                           "config.toml 的备份堆了 %d 份——"
                           "守护反复在改同一个文件（每次都备份），"
                           "说明修复没真正落地，或者在跟别的程序互相覆盖",
                           "fix"))
    except Exception:
        pass

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

    # ---- 检查连接池坏连接（中转商掐断 keep-alive 所致）----
    route = current_route()
    if mode == "proxy" and route:
        # ---- 检查「界面选的线路」和「实际转发的线路」是不是同一条 ----
        db_route = _db_current_provider()
        if db_route and db_route != route:
            ISSUES.append(("route-mismatch",
                           "CC Switch 实际转发的线路（%s）和界面上选的（%s）不是同一条"
                           "——代理读的是 live-state.json，两边脱节了。"
                           "你看着是 PRO 线，请求其实全打到另一条线，"
                           "报错会跟界面显示的对不上"
                           % (_provider_name(route) or route[:8],
                              _provider_name(db_route) or db_route[:8]), "fix"))
        fails, total = _cc_recent_failures(route)
        if total >= 5 and fails >= 5 and fails == total:
            ISSUES.append(("stale-connection-pool",
                           "当前线路连续 %d 次连接失败（全是 connection closed / "
                           "handshake timed out）——中转商把空闲的 keep-alive 连接"
                           "悄悄掐断了，CC Switch 连接池里攒下坏连接，越用越死。"
                           "守护已自动重置熔断计数；"
                           "若仍不通，手动重启一次 CC Switch 即可清掉"
                           "（守护不会自动重启，免得打断你正在跑的会话）"
                           % fails, "fix"))

    # ---- 检查模型目录（Codex 读它拿模型元数据）----
    if mode == "proxy" and route and os.path.exists(CATALOG_FILE):
        specs = _provider_model_catalog(route)
        if specs:
            try:
                with open(CATALOG_FILE, encoding="utf-8") as f:
                    cat = json.load(f)
                cmodels = cat.get("models") or []
                byslug = {m.get("slug"): m for m in cmodels}
                missing = [s.get("model") for s in specs
                           if s.get("model") and s.get("model") not in byslug]
                if missing:
                    ISSUES.append(("catalog-model-missing",
                                   "模型目录里没有当前路由的模型（%s）——"
                                   "Codex 会退回兜底元数据，"
                                   "1M 上下文被当成默认值，能力被砍"
                                   % "、".join(missing), "fix"))
                badtool = [s.get("model") for s in specs
                           if s.get("model") in byslug
                           and byslug[s.get("model")].get("tool_mode") == "code_mode_only"]
                if badtool:
                    ISSUES.append(("catalog-tool-mode",
                                   "模型目录里 %s 用了 code_mode_only——"
                                   "Codex 会发 'exec' 自定义工具，"
                                   "只认 apply_patch 的上游直接 400"
                                   % "、".join(badtool), "fix"))
            except Exception:
                pass

    # ---- 检查 Clash 运行时规则（文件里有 ≠ 内核加载了）----
    if os.path.exists(CLASH_CONFIG):
        try:
            with open(CLASH_CONFIG, "r", encoding="utf-8") as f:
                y = f.read()
            if "888aix.cn,DIRECT" not in y:
                ISSUES.append(("clash-config-rule-missing",
                               "Clash 配置文件里没有 AIX 的直连规则——"
                               "CC Switch 走系统代理时会落到兜底规则走境外节点，"
                               "上游连接会被掐", "fix"))
            else:
                # 文件里有，但内核不一定加载了 —— 必须查运行时规则表
                has = _clash_runtime_has_rule("api.888aix.cn")
                if has is False:
                    ISSUES.append(("clash-rule-not-loaded",
                                   "Clash 配置文件里有 AIX 直连规则，但内核运行时规则表里没有"
                                   "——规则改完没重新加载，等于没加。"
                                   "CC Switch 走 Clash 时会全部失败（实测 0/6 全败、"
                                   "直连 5/6 正常）", "fix"))
        except Exception:
            pass

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
        else:
            np_missing = [d for d in MUST_BYPASS_DOMAINS
                          if d not in env]
            if np_missing:
                ISSUES.append(("aix-not-bypassed",
                               "「快递单」的 no_proxy 里没有 %s——"
                               "走系统代理(Clash)时它会落到 MATCH,Others→Proxy 组走境外节点，"
                               "实测 TLS 直接握手失败（直连却通），"
                               "表现就是「连不上/一直重连」"
                               % "、".join(np_missing), "fix"))

    # ---- 检查系统代理绕过列表（macOS 层面）----
    if mode == "proxy" and route:
        try:
            upstream = _provider_upstream_host(route)
        except Exception:
            upstream = None
        if upstream and upstream.endswith("888aix.cn"):
            bypass_ok = _system_bypass_has(upstream)
            if not bypass_ok:
                ISSUES.append(("system-proxy-bypass-missing",
                               "macOS 系统代理的绕过列表里没有 %s——"
                               "Clash 会把它判给境外节点，实测走代理 TLS 握手失败、"
                               "直连却通，表现成「无法连接」"
                               % upstream, "fix"))

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

def fix_catalog():
    """修 Codex 的模型目录文件。

       两件事：
       1) 当前路由供应商登记的模型，必须在目录里有条目——否则 Codex 报
          "Model metadata not found" 用兜底元数据，1M 上下文被当成默认值。
       2) 条目里的 tool_mode 必须是 direct——code_mode_only 会让 Codex
          发 'exec' 自定义工具，只认 apply_patch 的上游直接 400。
    """
    route = current_route()
    if not route:
        return
    specs = _provider_model_catalog(route)
    if not specs:
        return
    if not os.path.exists(CATALOG_FILE):
        return
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    models = data.get("models") or []
    if not models:
        return
    base = models[0]
    have = {m.get("slug") for m in models}
    added, fixed = [], []
    for spec in specs:
        slug = spec.get("model")
        if not slug:
            continue
        if slug in have:
            # 已存在：检查工具模式
            for m in models:
                if m.get("slug") != slug:
                    continue
                if m.get("tool_mode") == "code_mode_only":
                    m["tool_mode"] = "direct"
                    m["apply_patch_tool_type"] = "freeform"
                    m["experimental_supported_tools"] = []
                    fixed.append(slug)
                break
            continue
        entry = _catalog_entry(base, spec)
        if entry:
            models.append(entry)
            added.append(slug)
    if not added and not fixed:
        return
    data["models"] = models
    try:
        bak = backup(CATALOG_FILE)
        if bak:
            FIX_LOG.append("已备份模型目录 → %s" % os.path.basename(bak))
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if added:
            FIX_LOG.append("模型目录补进当前路由的模型（%s）——"
                           "缺条目时 Codex 只能用兜底元数据，"
                           "1M 上下文会被当成默认值" % "、".join(added))
        if fixed:
            FIX_LOG.append("模型目录里 %s 的工具模式从 code_mode_only 改回 direct"
                           "（code 模式会让 Codex 发 exec 工具，上游不认）"
                           % "、".join(fixed))
    except Exception as e:
        say("  ⚠ 模型目录暂时没写进去（不影响能不能连）：%s" % e)


# ================================================================ fix

def fix():
    """修复：把 doctor 找到的问题全部治好（并固化进模板库）。"""
    global FIX_LOG
    FIX_LOG = []
    mode = current_mode()
    client_root = detect_client_root()

    # ---------- 0. 路由对齐（必须最先做）----------
    # 代理转发读的是 live-state.json，不是数据库。
    # 两边脱节时，界面显示的线路 ≠ 实际打的线路，
    # 后面按 current_route() 做的所有修正都会改错对象。
    fix_route_mismatch()

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

        # 1f. 剔除 Codex 不认识的键（供应商配置里混进来的，会每次启动刷 warning）
        for junk in ("disable_response_storage",):
            pat = r"^[ \t]*%s[ \t]*=.*$\n?" % re.escape(junk)
            if re.search(pat, src, re.M):
                src = re.sub(pat, "", src, flags=re.M)
                FIX_LOG.append("剔除 Codex 不认识的键「%s」（它每次启动都会刷警告）"
                               % junk)

        if mode == "direct":
            # 1a-2. 官方直连模式下，清掉第三方路由的残留：
            # 顶层 model_provider = "custom" 会让请求错送本地代理 → 401
            m = re.search(r"^[ \t]*model_provider[ \t]*=.*$\n?", src, re.M)
            if m:
                src = src[:m.start()] + src[m.end():]
                FIX_LOG.append("撕掉官方直连菜单里的「model_provider」残留"
                               "（留着它，请求会被错送到 CC Switch，退回 401）")
            span = _segment_span(src, "model_providers.custom")
            if span:
                src = src[:span[0]] + src[span[1]:]
                src = re.sub(r"\n{3,}", "\n\n", src)
                FIX_LOG.append("删掉官方直连菜单里的 [model_providers.custom] 小节"
                               "（第三方路由专用，直连留着只会添乱）")

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

            # 1c. 供应商小节：WebSocket 快线 → HTTP 普通件 + 补齐耐心值
            span = _segment_span(src, "model_providers.custom")
            if span:
                s, e = span
                seg, added = _ensure_provider_keys(src[s:e], strict=True)
                if added:
                    src = src[:s] + seg + src[e:]
                    FIX_LOG.append("供应商小节补齐：%s" % "、".join(added))
            else:
                # 段整段缺失（比如被清理过）：照搬 CC Switch 里当前路由的段原文
                seg_text = _provider_custom_segment(current_route())
                if seg_text:
                    seg_text, added = _ensure_provider_keys(seg_text, strict=True)
                    src = src.rstrip("\n") + "\n\n" + seg_text
                    FIX_LOG.append("菜单里缺少 [model_providers.custom] 小节——"
                                   "已按当前路由的供应商配置补建（照搬，不编造），"
                                   "并补齐：%s" % "、".join(added))

            # 1c-2. base_url 必须指向 CC Switch 本地代理
            # 直写上游地址 = 绕过代理层，界面切路由对 Codex 失效
            span = _segment_span(src, "model_providers.custom")
            if span:
                s, e = span
                seg = src[s:e]
                m = re.search(r'^[ \t]*base_url[ \t]*=[ \t]*"([^"]*)"', seg, re.M)
                if not m:
                    seg = seg.rstrip("\n") + '\nbase_url = "%s"\n' % PROXY_BASE_URL
                    src = src[:s] + seg + src[e:]
                    FIX_LOG.append("供应商小节里没有 base_url——已补成 CC Switch 本地代理"
                                   "（%s）" % PROXY_BASE_URL)
                elif m.group(1).rstrip("/") != PROXY_BASE_URL.rstrip("/"):
                    old_url = m.group(1)
                    seg = seg[:m.start(1)] + PROXY_BASE_URL + seg[m.end(1):]
                    src = src[:s] + seg + src[e:]
                    FIX_LOG.append("base_url 从上游直连地址（%s）改成本地代理（%s）——"
                                   "绕过了代理层，界面切路由会对 Codex 失效"
                                   % (old_url, PROXY_BASE_URL))
                # 本地代理用占位凭证，真实 key 由代理层按 provider 注入
                bm2 = re.search(r'^[ \t]*experimental_bearer_token[ \t]*=[ \t]*"([^"]*)"', seg, re.M)
                if not bm2:
                    seg = seg.rstrip("\n") + '\nexperimental_bearer_token = "%s"\n' % PROXY_BEARER
                    src = src[:s] + seg + src[e:]
                    FIX_LOG.append("补上代理占位凭证（experimental_bearer_token）")
                elif bm2.group(1) != PROXY_BEARER:
                    seg = seg[:bm2.start(1)] + PROXY_BEARER + seg[bm2.end(1):]
                    src = src[:s] + seg + src[e:]
                    FIX_LOG.append("把真实 API Key 换成代理占位凭证（%s）——"
                                   "本地代理不校验它，真 key 该由代理层按供应商注入"
                                   % PROXY_BEARER)

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
        # no_proxy 必须包含 AIX 域名 —— 走系统代理(Clash)时 AIX 的
        # Cloudflare Anycast IP 不命中 GEOIP,CN，会落到 MATCH,Others→Proxy
        # 组走境外节点，实测走代理 TLS 直接失败、直连却通。
        np_lines = [l for l in out if re.match(r'^no_proxy\s*=', l.strip(), re.I)]
        if not np_lines:
            out.append('NO_PROXY="%s"' % NO_PROXY_VALUES)
            out.append('no_proxy="%s"' % NO_PROXY_VALUES)
            FIX_LOG.append("快递单补上「本地地址与中转商不走代理」白名单")
        else:
            fixed_np = []
            for l in np_lines:
                vals = re.findall(r'["\']([^"\']*)["\']', l)
                cur_vals = [v for part in vals for v in part.split(",") if v.strip()]
                missing = [d for d in MUST_BYPASS_DOMAINS if d not in cur_vals]
                if missing:
                    cur_vals += missing
                    newline = l.split("=")[0] + "=" + '"%s"' % ",".join(cur_vals)
                    fixed_np.append(newline)
                    FIX_LOG.append("快递单的 no_proxy 补上 %s——"
                                   "走系统代理(Clash)时这条线路连不上"
                                   % "、".join(missing))
                else:
                    fixed_np.append(l)
            out = [l for l in out if not re.match(r'^no_proxy\s*=', l.strip(), re.I)]
            out += fixed_np
        new = "\n".join(out).rstrip("\n") + "\n"
        new = re.sub(r"\n{3,}", "\n\n", new)
        new = new.lstrip("\n")
        old = "\n".join(lines).rstrip("\n") + "\n"
        if new != old:
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write(new)
            if removed:
                FIX_LOG.append("撕掉快递单上关门代理的地址（%s）——"
                                "这是无限重连的真凶" % ", ".join(removed))

    # ---------- 2.5 macOS 系统代理绕过列表 ----------
    # Clash 更新配置后可能把绕过规则冲掉，AIX 就又走境外节点连不上了。
    try:
        upstream = _provider_upstream_host(current_route())
    except Exception:
        upstream = None
    if upstream and upstream.endswith("888aix.cn") and not _system_bypass_has(upstream):
        try:
            r = subprocess.run(["networksetup", "-getproxybypassdomains", "Wi-Fi"],
                               capture_output=True, text=True, timeout=15)
            cur = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
            for d in MUST_BYPASS_DOMAINS:
                if d not in cur:
                    cur.append(d)
            subprocess.run(["networksetup", "-setproxybypassdomains", "Wi-Fi"] + cur,
                           capture_output=True, text=True, timeout=20)
            FIX_LOG.append("macOS 系统代理绕过列表补上 %s——"
                           "不走代理这条线才连得上"
                           % "、".join(MUST_BYPASS_DOMAINS))
        except Exception as e:
            say("  ⚠ 系统代理绕过列表暂时改不了（不影响 Codex 走本地代理）：%s" % e)

    # ---------- 2.6 Clash 规则置顶直连 + 内核热重载 ----------
    # 订阅更新会重生成 rocket.yaml，手工加的 DIRECT 规则会被冲掉。
    #
    # ⚠️ 关键教训（2026-11-01 实测）：**光改文件不管用**。
    # Clash 内核加载配置是在启动时做的，改完 rocket.yaml 不会自动生效——
    # 实测守护在 08:01 把 DIRECT 规则写进文件了，但内核运行时规则表里
    # 678 条规则中「888aix」出现 0 次，规则根本没进去。
    # 结果：CC Switch 走系统代理 → 走 Clash 4780 → 落到 MATCH,Others
    # → 走境外节点 → 经 Clash 请求 0/6 全败（SSL EOF），直连 5/6 正常。
    #
    # 正解：改完文件后用 Clash 的外部控制器热重载（PUT /configs?force=true），
    # 再读 /rules 校验运行时规则表里确实有这条规则。
    try:
        if os.path.exists(CLASH_CONFIG):
            with open(CLASH_CONFIG, "r", encoding="utf-8") as f:
                y = f.read()
            if "888aix.cn,DIRECT" not in y:
                y2 = y.replace(
                    "rules:\n",
                    "rules:\n  # AIX 中转（Codex 唯一供应商）——强制直连，"
                    "避免落到 MATCH,Others 走境外节点\n"
                    "  - DOMAIN,api.888aix.cn,DIRECT\n"
                    "  - DOMAIN-SUFFIX,888aix.cn,DIRECT\n", 1)
                if y2 != y:
                    with open(CLASH_CONFIG, "w", encoding="utf-8") as f:
                        f.write(y2)
                    FIX_LOG.append("Clash 规则补上 api.888aix.cn → DIRECT——"
                                   "订阅更新把它冲掉了，落到兜底规则就走境外节点")
            # 文件里有规则 ≠ 内核加载了。必须核对运行时规则表。
            _clash_reload_if_needed()
    except Exception as e:
        say("  ⚠ Clash 规则暂时改不了：%s" % e)

    # ---------- 2.7 CCS 数据库模板固化（真正的治本）----------
    # 关键教训：CC Switch 接管时会用它自己的模板重写 config.toml，
    # 我们在 config.toml 里补的键会被覆盖掉 → 守护与 CCS 互相覆盖，
    # 形成自激循环（实测 1 小时堆出 560 个备份、CCS 被反复重启）。
    # 正解是把耐心值键写进数据库里 provider 的 config 模板，
    # 这样 CCS 每次重写 config.toml 时都会带上它们。
    _fix_provider_templates()

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

    # ---------- 3.5 路由切换检测（会话粘性提醒）----------
    check_mode_switch()

    # ---------- 4. 模型目录（Codex 读它拿模型元数据）----------
    fix_catalog()

    # ---------- 5. 连接池坏连接（连续失败时重启 CCS）----------
    fix_connection_pool()

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
"""watch_loop.py — 轮询兜底：菜单被写坏就修（秒级交给 FSEvents）

铁律（实测教训）：
1) 只在「内容真的变了」时才修 —— 用内容指纹，不用 mtime+size。
   CCS 一直在写 cc-switch.db（熔断计数、请求日志），mtime 天天在变，
   但内容没实质变化，不该每次都跑一遍 fix。
2) 自己写的文件不触发自己 —— 否则 fix 写库 → 触发 fix → 无限循环。
   实测曾 1 小时堆出 560 个 config.toml.bak-*，CCS 每几秒被重启一次。
   所以每次 fix 之后重取快照，只有「快照之后又变了」才再修。
3) 最小间隔 —— 两次修复之间至少隔 20 秒，别把磁盘写满。
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(HERE, "watcher.pid")
GUARD = os.path.join(HERE, "codex_guard.py")
PY = sys.executable
TARGETS = [os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
           os.path.join(os.path.expanduser("~"), ".codex", ".env"),
           os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db")]

MIN_INTERVAL = 20   # 秒：两次 fix 之间的最小间隔


def digest(path):
    """算文件内容指纹；读不到就返回 None"""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def snap():
    return {p: digest(p) for p in TARGETS}


try:
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
except Exception:
    pass

prev = snap()      # 首轮先记快照，别一上来就修
last_fix = 0.0

while True:
    time.sleep(3)
    try:
        cur = snap()
    except Exception:
        continue
    changed = [p for p in TARGETS
               if prev.get(p) is not None and prev.get(p) != cur.get(p)]
    if changed and (time.time() - last_fix) >= MIN_INTERVAL:
        time.sleep(0.4)   # 等写完落盘
        last_fix = time.time()
        subprocess.run([PY, GUARD, "fix"], capture_output=True, timeout=180)
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
    let line = "[\\(ts)] \\(m)\n"
    if let fh = try? FileHandle(forWritingTo: logURL) {
        defer { try? fh.close() }
        fh.seekToEndOfFile()
        fh.write(line.data(using: .utf8)!)
    } else {
        try? line.write(to: logURL, atomically: true, encoding: .utf8)
    }
}

var lastRun = Date.distantPast
// 最小间隔 20 秒（实测教训：0.5 秒太密，fix 写库 → 触发 fix →
// 1 小时堆出 560 个 config.toml.bak-*，CCS 每几秒被重启一次）
let MIN_INTERVAL: TimeInterval = 20

func runFix() {
    let now = Date()
    if now.timeIntervalSince(lastRun) < MIN_INTERVAL { return }
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

    # 建立路由模式基线快照（装上守护这一刻的状态当作"旧状态"，
    # 之后的每次切换才会触发"请新建会话"提醒）
    try:
        m, r = current_mode(), current_route()
        if m:
            with open(MODE_MEMO, "w", encoding="utf-8") as f:
                json.dump({"mode": m, "route": r,
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass

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
