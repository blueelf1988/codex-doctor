# 🩺 Codex Doctor — Codex 连不上？一条命令治好它

> 你是不是也遇到过这些抓狂的时刻——
>
> **"Codex 一直在转圈圈：Reconnecting... waiting for network"**
> **"切换了 CC Switch 的模型，Codex 突然就废了"**
> **"升级了客户端，第二天全都连不上"**
> **"Work browser access requires an authenticated account？？？"**
>
> 别重装了。多半不是你的问题，是几份配置文件被写坏了。这个工具能自动找出来、自动治好、并且让它们**以后也坏不了**。

---

## 一、这是什么？（说人话版）

你的 Codex 每次启动，都会读一份"**点菜单**"（`~/.codex/config.toml`），照着菜单决定"连哪家店、用什么钥匙"。

而 CC Switch 每次切换模型，都会**重写这份菜单**。问题就出在这——重写的时候，经常把菜单写坏：

| 菜单被写坏的样子 | 结果 |
|---|---|
| 菜单上开了"Work 模式"（要 ChatGPT 会员账号才能点的菜） | 报 Work browser access 错误 |
| 地址写的是"已经关门的老店"（旧版客户端路径） | 升级后功能全失灵 |
| 菜单上还留着上一家店的"钥匙孔"（model_provider 残留） | 你明明用官方直连，它却跑去敲 CC Switch 的门，被 401 退回 |
| 钥匙和锁不配（模型名和路由对不上） | 报 unknown model |
| "快递单"上写着一个**已经关门的代理地址** | 所有请求都被送去这个空地址 → **无限转圈重连**（最常见！） |

**Codex Doctor 做三件事：**

1. **体检（doctor）**——把菜单从头到尾检查一遍，用大白话告诉你哪里坏了（不动手）
2. **修复（fix）**——当场把坏的地方全改好，改之前自动备份
3. **装守护（install）**——派一个"自动纠察队"住在系统里：菜单一被写坏，**秒级**自动修好。以后你切模型、升级客户端，随便折腾，都不用再管

**治本的关键**：它不只会修菜单，还会把正确写法**存进 CC Switch 的"菜单模板库"**——以后 CC Switch 每次重写菜单，写出来的天然就是对的。这是和"改一次管一阵"的根本区别。

---

## 二、使用办法（3 步，全程 1 分钟）

### 前提
- macOS（Apple Silicon / Intel 都行）
- 装了 OpenAI 官方的 **ChatGPT.app**（新版 Codex 桌面端就内嵌在里面）
- 可选：装了 **CC Switch**（用来切换第三方模型路由的工具）

### 第 1 步：下载

```bash
git clone https://github.com/blueelf1988/codex-doctor.git
cd codex-doctor/scripts
```

### 第 2 步：体检（看看问题在哪，不改任何东西）

```bash
python3 codex_guard.py doctor
```

> 💡 什么都没有也照样能跑——它会告诉你"一切健康"，或者列出所有毛病（用大白话）。

### 第 3 步：一键治好 + 装上自动守护

```bash
python3 codex_guard.py install
```

这一条命令会自动完成：修好当前所有问题 → 派驻"自动纠察队"（秒级响应）→ 配置开机自启（守护挂了系统会自动拉起来）。

**装完之后，你就再也不用管了。** 切路由、升级客户端、重启电脑，都自动保持健康。

### 只想修一次、不装守护？

```bash
python3 codex_guard.py fix        # 只修一次
```

### 不想要了？

```bash
python3 codex_guard.py uninstall  # 一键卸载，菜单本身不动，Codex 照常能用
```

---

## 三、常见问题：根因 & 解决办法（全表）

### 症状速查表

| # | 你看到的报错 | 真正的原因（说人话） | 怎么解决 |
|---|---|---|---|
| 1 | **Reconnecting... waiting for network** 无限转圈 | **90% 是这个**：`~/.codex/.env`（快递单）上写着一个**已经关门的代理地址**（如 `HTTP_PROXY=127.0.0.1:7890`，代理软件退出了但地址还留在单子上）——Codex 把所有请求都送去这个空地址，等一辈子也等不到 | `fix` 自动撕掉死代理地址，并补上"本地地址不走代理"的白名单 |
| 2 | **Work browser access requires an authenticated account** | 菜单上开了"Work 模式"（`conversationDetailMode = STEPS_PROSE`）——这是一道**必须 ChatGPT 账号登录才能点的菜**，用第三方模型钥匙点不了 | `fix` 自动改回普通模式（STEPS_COMMANDS），并固化进模板库，以后不会再被写坏 |
| 3 | **401：已切换到 OpenAI 官方供应商，但这个请求仍按第三方供应商的配置发出** | 你切回了**官方直连**，但菜单上还残留着**第三方路由的写法**（`model_provider="custom"`）——Codex 照着旧菜单去敲 CC Switch 的门，被退回 | `fix` 是**模式感知**的：官方直连时清掉第三方写法，路由模式时补齐它。你切到哪种模式，它就保持哪种模式的正确写法 |
| 4 | **unknown model 'xxx'** | 菜单上写的**模型名**还是上一家店的（切了路由但菜单没跟着换锁） | `fix` 自动把菜单上的模型名改成当前路由的 |
| 5 | **升级客户端后突然全废了** | 客户端从 Codex.app **搬家**到了 ChatGPT.app，但菜单里还写着**老地址**（MCP 工具、node_repl 全指向空地址） | `fix` 自动把所有老地址改写成新地址（自动探测实际安装位置） |
| 6 | **failed to connect to websocket: ... wss://api.openai.com/...** | 新版 Codex 默认走"**WebSocket 快线**"送请求，但 CC Switch 只收"**HTTP 普通件**"——快线的地址还是硬编码到官方的，绕过了你配的路由 | `fix` 在供应商小节加一行 `supports_websockets = false`，改回普通件 |
| 7 | 426 Upgrade Required / websocket_not_supported | 同上：CC Switch 明确说"我这儿不收快线" | 同上 |
| 8 | **warning: Codex is ignoring ... type=stdio** 每次启动都刷 | 菜单模板库里混进了一个 Codex 不认识的写法（那是 Claude Code 的格式，串门了） | `fix` 自动剔除 |
| 9 | 报错说配置解析失败（Invalid statement at line 1） | 菜单开头被塞了个**看不见的隐形字符**（UTF-8 BOM）——就像信封上贴了张隐形邮票，机器读直接卡住 | `fix` 自动剥掉 |
| 10 | 切完路由，Codex 好像"没反应过来"，要等一会儿才正常 | 菜单被 CC Switch 重写的瞬间有一个"**空窗期**"——旧守护方案要等轮询发现才修，你在这几秒发消息就会失败 | `install` 装的 FSEvents 监听器是**秒级**响应（菜单一落盘就修），实测**同秒完成**；且模板库已被固化，**多数情况根本不会产生坏菜单** |

### 进阶问答

<details>
<summary><b>Q：它会不会乱动我的东西？</b></summary>

不会。三条铁律：

1. **绝不删改你在 CC Switch 里手动建的供应商记录**——你在 CC Switch 界面里看到的每一家店、每一把钥匙，一个字都不会动
2. **绝不改你选的模式**——官方直连还是第三方路由，你自己切；它只保证"当前模式下菜单的写法是正确的"
3. **改任何文件前自动备份**——每次修复都留带时间戳的备份，可回滚
</details>

<details>
<summary><b>Q：为什么修一次不管用，非要装守护？</b></summary>

因为病根不在菜单本身，而在"**写菜单的人**"（CC Switch 每次切换都会重写）。只修一次，下次切路由可能又被写坏。

所以本工具是三层治疗：
1. **模板库固化**（治本）——把正确写法存进 CC Switch 的模板库，它以后写出来的菜单天然就对
2. **当场修复**（治标）
3. **秒级监听**（保险）——万一模板库覆盖不到的边角情况，监听器秒级补刀

第 1 层是关键：多数问题在源头就不会发生了。
</details>

<details>
<summary><b>Q：支持哪些"第三方模型"路由？</b></summary>

实测过的：MiniMax、OpenCode Go、OpenAI 官方直连（个人账号）。原理上是通用的——只要 CC Switch 能切换，本工具的模式感知逻辑就能适配（读 CC Switch 自己的 `live-state.json` 判断当前是哪种模式，不依赖具体供应商）。
</details>

<details>
<summary><b>Q：Windows / Linux 能用吗？</b></summary>

**doctor / fix 可以用**（纯 Python，跨平台）。**install 的开机自启部分目前只支持 macOS**（依赖 launchd）。Windows/Linux 用户可以每次遇到问题时跑一遍 `fix`，或者把 `fix` 挂进自己的定时任务。
</details>

<details>
<summary><b>Q：卸载会留下什么？</b></summary>

什么都不留：停进程、删自启、清目录，一步到位。你的 `config.toml` 菜单本身不动，Codex 照常用。
</details>

---

## 四、它都改了什么？（透明清单）

| 文件 | 干什么用 | 动不动它 |
|---|---|---|
| `~/.codex/config.toml` | Codex 的"点菜单" | **会修**（改前备份） |
| `~/.codex/.env` | 子进程的"快递单" | **会修**（改前备份） |
| CC Switch 数据库的 `common_config_codex` | CC Switch 的"菜单模板库" | **会修**（只改通用模板，不碰你的供应商记录） |
| CC Switch 数据库的 `providers` 表 | 你手动建的每一家供应商 | **绝不碰** |
| `~/.codex/leo-guard/` | 自动纠察队驻地 | install 时创建，uninstall 时删除 |
| `~/Library/LaunchAgents/com.user.codex-guard.plist` | 开机自启配置 | install 时创建，uninstall 时删除 |

---

## 五、命令一览

```bash
python3 codex_guard.py doctor     # 体检：找出所有问题（只看不动手）
python3 codex_guard.py fix       # 修复：当场治好 + 固化模板
python3 codex_guard.py install   # 修复 + 装自动守护（推荐，一次到位）
python3 codex_guard.py uninstall # 卸载守护（菜单不动）
```

---

## 六、免责与适用范围

- 适用于 **ChatGPT.app（内嵌 Codex 桌面端）+ CC Switch** 的组合场景
- 全部修复动作有备份、可回滚，但**任何系统级工具使用前，建议你先跑 `doctor` 看清它要改什么**
- 本工具不收集任何数据、不联网（除了 GitHub 这份代码本身）
- 遇到没见过的报错？跑 `doctor` 把输出贴到 [Issues](https://github.com/blueelf1988/codex-doctor/issues)，我来帮你分析

---

> **▶ Insight｜为什么"重装大法"对这类问题没用？**
> 因为重装换的是"厨房"，坏的是"菜单"。菜单是重装后第一次启动就生成的，CC Switch 一接管又写坏。治菜单才是治根。

## License

MIT
