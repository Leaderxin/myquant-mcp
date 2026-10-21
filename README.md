# myquant-mcp

**通过运行中的掘金量化终端（GM Terminal）提供实时行情与 K 线数据的 MCP Server。**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.x-green)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)

配合 Claude Desktop / Cursor / Claude Code 等支持 MCP 的客户端，让 AI 直接读取你本地掘金终端的行情数据，用于技术分析、结构识别、复盘等场景。

---

## 为什么需要它

掘金量化有官方 Python SDK（`gm`），但它有两个门槛：

1. **数据服务依赖掘金终端**——终端是 Windows GUI 程序，必须常驻运行；
2. **SDK 是同步阻塞接口**——LLM 工具调用需要异步，且返回的 `datetime`/`numpy`/`NaN` 无法直接序列化。

本项目在两者之间做一层薄封装：**连接管理 + 异步包装 + 数据规整 + 错误翻译**，把掘金终端变成一个 AI 可直接调用的 MCP 工具集。

---

## 前置条件

| 条件 | 说明 |
|---|---|
| **掘金终端已运行** | Windows 客户端，启动并完成登录。官方下载：https://www.myquant.cn/ |
| **GM Token** | 终端内「系统设置 → 密钥管理」复制 |
| **Python 3.10+** | 本服务与终端可以不在同一台机器（见「跨机器部署」） |

> ⚠️ **掘金终端本身只能在 Windows 上运行**。如果你在 Linux/macOS 或容器里跑 MCP 服务，
> 需要把 `GM_SERV_ADDR` 指向那台 Windows 机器的 `IP:7001`。

---

## 安装

```bash
git clone https://github.com/Leaderxin/myquant-mcp.git
cd myquant-mcp

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

国内网络可加速掘金 SDK：

```bash
pip install gm -i https://mirrors.aliyun.com/pypi/simple/
```

---

## 配置

复制 `.env.example` 为 `.env` 并填写：

```env
GM_TOKEN=你的掘金token
GM_SERV_ADDR=127.0.0.1:7001
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `GM_TOKEN` | ✅ | — | 掘金终端密钥管理中的 token |
| `GM_SERV_ADDR` | ❌ | `127.0.0.1:7001` | 终端地址。跨机器填 `<终端IP>:7001` |
| `MCP_TRANSPORT` | ❌ | `stdio` | `stdio` / `streamable-http` / `sse` |

> 本服务**不读取** `.env` 文件，请让客户端通过 `env` 字段注入（下面有示例），
> 或在启动前 export 到环境变量。

### Claude Desktop

编辑 `claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/`；
Windows：`%APPDATA%\Claude\`）：

```json
{
  "mcpServers": {
    "myquant": {
      "command": "python",
      "args": ["/绝对路径/myquant-mcp/server.py"],
      "env": {
        "GM_TOKEN": "你的token",
        "GM_SERV_ADDR": "127.0.0.1:7001"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add myquant \
  --command python \
  --args /绝对路径/myquant-mcp/server.py \
  --env GM_TOKEN=你的token \
  --env GM_SERV_ADDR=127.0.0.1:7001
```

### Cursor

`~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "myquant": {
      "command": "python",
      "args": ["/绝对路径/myquant-mcp/server.py"],
      "env": { "GM_TOKEN": "你的token" }
    }
  }
}
```

---

## 提供的工具

| 工具 | 用途 |
|---|---|
| `ping_gm_terminal` | 连通性自检：区分「终端没开 / 地址错 / token 失效」 |
| `get_realtime_quote` | 实时行情快照（支持多标的、买卖五档） |
| `get_kline_history` | 指定时间段的历史 K 线 |
| `get_kline_recent` | 最近 N 条 K 线（不用算时间区间） |
| `normalize_symbol_code` | 代码格式转换（`600000` → `SHSE.600000`） |
| `list_supported_frequencies` | 列出支持的频率与数据范围 |

**建议用法**：先 `ping_gm_terminal` 探活，再取数。AI 客户端会自己按需调用。

### 参数速查

**标的代码**支持多种写法，自动规范化：

```
600000          → SHSE.600000
sh600000        → SHSE.600000
600000.SH       → SHSE.600000
SHSE.600000     → SHSE.600000
000001          → SZSE.000001
```

**频率**（`frequency`）：

| 值 | 含义 |
|---|---|
| `tick` | 快照行情 |
| `60s` / `300s` / `900s` / `1800s` / `3600s` | 1 / 5 / 15 / 30 / 60 分钟 |
| `1d` | 日线 |

**复权**（`adjust`）：`none`（默认）/ `prev`（前复权，技术分析推荐）/ `post`

---

## 数据范围与限制

以下为掘金免费行情权限的实际情况，编码前请先了解，避免踩坑：

| 项目 | 限制 |
|---|---|
| **周线 / 月线 / 年线** | ❌ **不支持**（官方 FAQ 明确说明），需用日线本地合成 |
| 分钟线历史范围 | 约从 2016 年起 |
| 60s 以下周期 | ❌ 不支持（最小 60s） |
| Tick 历史 | 仅最近约 **3 个月** |
| 单次返回上限 | **33,000 条**，超出部分不返回，需分段查询 |
| 集合竞价 | 实时模式下无法获取 |
| 实时推送频率 | 约每 3 秒一次快照 |

**做多级别技术分析时的建议**：日线用于大级别定方向，分钟线用于小级别结构；
需要周/月线时由日线聚合（本服务未内置，避免掩盖数据口径问题）。

---

## 跨机器部署

MCP 服务与掘金终端不在同一台机器时（例如终端在 Windows，服务在 Linux 容器）：

```
┌─────────────────┐         ┌──────────────────┐
│  掘金终端        │◄────────│  myquant-mcp     │
│  Windows        │  7001   │  Linux / 容器     │
│  0.0.0.0:7001   │         │  GM_SERV_ADDR=   │
└─────────────────┘         │  <终端IP>:7001   │
                            └──────────────────┘
```

1. 确认终端监听 `0.0.0.0:7001` 而非仅 `127.0.0.1`；
2. Windows 防火墙放行 7001；
3. 设置 `GM_SERV_ADDR=<终端IP>:7001`；
4. **Docker Desktop 场景**用 `host.docker.internal:7001`，不要用 `127.0.0.1`
   （容器内 `127.0.0.1` 指向容器自身）。

验证端口：`curl -v telnet://<终端IP>:7001` 或 `nc -zv <终端IP> 7001`。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `缺少 GM_TOKEN` | 未配置 token，到终端「系统设置 → 密钥管理」复制 |
| `连接掘金终端失败 ... 无法连接到终端服务` | 终端未登录 / `GM_SERV_ADDR` 错误 / 防火墙拦截 / 容器内用了 127.0.0.1。按错误提示逐条排查 |
| `掘金鉴权失败` | token 失效（重新生成后旧的立即作废），或 token 与终端账号不匹配 |
| 返回空数据 | 标的代码错、非交易时段、或查询区间超出历史范围 |
| 返回条数正好 33000 | 触达单次上限，缩小区间分段取 |

---

## 开发

```bash
pip install -r requirements.txt

# 跑测试（78 个用例，含 Chinese 错误翻译、DataFrame 序列化、符号规范化）
pytest -q

# 本地直接跑（stdio）
GM_TOKEN=xxx GM_SERV_ADDR=127.0.0.1:7001 python server.py
```

代码结构：

```
myquant-mcp/
├── server.py                  # MCP 入口 + 6 个工具定义
├── src/myquant_mcp/
│   ├── client.py              # gm SDK 封装：连接管理、错误翻译、符号规范化
│   └── serialize.py           # datetime / numpy / NaN → JSON 安全类型
└── tests/
    ├── test_client.py         # 符号规范化、参数校验、SDK 调用（mock）
    └── test_serialize.py      # 各类数据转换
```

**设计取舍**：

- **懒连接**：`set_serv_addr` / `set_token` 只在首次取数时执行，避免 import 阶段因终端未启动而崩溃；
- **同步 SDK 包进线程池**：`asyncio.to_thread`，不阻塞 MCP 事件循环；
- **错误分层翻译**：掘金错误信息中英混杂（`{"status": 1001, "message": "无法连接到终端服务"}`），
  关键词表必须同时覆盖中英文，否则会被当成未知错误（这条是实测踩出来的）；
- **空字符串字段剔除**：掘金在字段无效时填 `''`，但 `0` 必须保留（可能是真实成交量）。

---

## 许可

[MIT](LICENSE)

## 相关

- [掘金量化官网](https://www.myquant.cn/)
- [掘金量化 API 文档](https://www.myquant.cn/docs2/sdk/python/API%E4%BB%8B%E7%BB%8D.html)
- [Model Context Protocol](https://modelcontextprotocol.io/)
