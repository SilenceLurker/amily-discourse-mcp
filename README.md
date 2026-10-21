# Discourse 多账号本地 MCP

为不同模型和 Agent 提供统一的 Discourse 内容工具。每次操作明确传入账号别名，每个别名绑定自己的站点、用户名、加密 API Key、分区和动作权限。首版支持 Windows、stdio；多个客户端各自启动进程，共用同一份本地 SQLite 状态，不需要常驻 HTTP 端口。

当前版本为 0.1.0，支持独立部署。真实论坛接入需要操作者在本地终端录入有效凭据。

## 安装与本机状态

需要 Windows CPython 3.11+，本次实际使用 CPython 3.12.14 和官方 MCP Python SDK 2.3.0。`requirements.lock` 固定了本次 Windows 3.12 环境验证过的依赖版本；它是版本快照，不是带下载哈希的供应链锁文件。

```powershell
git clone https://github.com/SilenceLurker/amily-discourse-mcp.git
cd amily-discourse-mcp
.\setup.ps1 -PythonExe 'C:\path\to\python.exe'
.\.venv\Scripts\python.exe -m discourse_mcp --help
```

安装后使用项目内的 `.venv\Scripts\python.exe`。默认状态目录为 `%LOCALAPPDATA%\Amily\DiscourseMCP`。也可以在子命令前指定 `--state-dir 'D:\private\DiscourseMCP'`；所有共用凭据的客户端必须指向相同目录，并以相同 Windows 用户运行。不要把状态目录放在 Git 仓库、共享目录或同步盘中。

数据库保存账号元数据、DPAPI 密文、草稿及发布记录。**只有 API Key 加密，草稿正文和发布元数据没有加密**；未披露审核稿也应保存在受控的用户目录中。备份数据库前先停止所有客户端，避免遗漏 WAL 中尚未合并的数据。更换 Windows 用户或机器后通常需要重新录入 Key。

## 录入多个账号

在 Discourse 管理端分别创建绑定具体用户的 Single User API Key，授予所需的最小接口权限。站内账号本身的分区权限仍需配置。不要用管理员 All Users Key 代替每个账号自己的 Key。不同站点可以使用不同别名，用户名相同也不影响区分。

以下命令只保存配置，Key 由本地终端隐藏输入，不写入命令行、环境变量或 MCP 参数。运行命令不会自动连论坛校验 Key，也不会发帖。

```powershell
$taskPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path

# 替换示例站点、用户名和分区 ID；每个别名单独输入对应 Key。
& $taskPython -m discourse_mcp account-add audit --site https://forum.example.com --username audit-bot --categories 60

# 第二个账号；替换为实际用户名及允许操作的分区。
& $taskPython -m discourse_mcp account-add personal --site https://forum.example.com --username YOUR_USERNAME --categories 10,20 --actions read,create,reply,edit

& $taskPython -m discourse_mcp accounts
```

每个账号默认要求对具体草稿进行本地批准。普通内容确实需要无人值守发布时，可以在创建对应账号时明确加 `--allow-unattended`。安全公告账号应保留默认审核要求。现有账号的站点、用户名、权限配置不可被模型修改；需要另一组权限时建立新别名并停用旧别名。

本地凭据维护：

```powershell
& $taskPython -m discourse_mcp account-rotate audit
& $taskPython -m discourse_mcp account-disable audit
& $taskPython -m discourse_mcp account-enable audit
```

轮换在本地替换密钥，**不会撤销论坛上的旧 Key**；请另行在 Discourse 撤销旧 Key。轮换和启停都会改变账号版本，之前的待发布草稿必须重新保存并审核。

## 对接 MCP 客户端

将 `mcp-config.example.json` 中的 server 配置加入支持 stdio 的客户端，将示例 `C:\Projects\amily-discourse-mcp` 替换为实际项目绝对路径，并按该客户端自己的配置格式调整。示例只开放 `audit`；确实要让一个客户端操作多个账号，改成 `--accounts audit,personal`。也可以为不同 Agent 分别只开放一个别名。

配置中只有可执行文件、参数和账号别名，没有 API Key。首次录入后，同一 Windows 用户的多个 MCP 客户端可以复用它，直到 Key 过期、撤销或轮换。服务不保证 API Key 永久有效。

使用自定义状态目录时，参数顺序如下：

```json
["-m", "discourse_mcp", "--state-dir", "D:\\private\\DiscourseMCP", "serve", "--accounts", "audit,personal"]
```

工具列表：

| 工具 | 行为 |
| --- | --- |
| `discourse_accounts` | 列出本进程开放的账号及权限，无凭据 |
| `discourse_read_topic` | 读取允许分区内普通主题的首批帖子 |
| `discourse_read_post` | 读取单个帖子原文，核对其主题分区 |
| `discourse_prepare_draft` | 本地保存新主题、回复或编辑草稿；不发布 |
| `discourse_get_draft` | 读取草稿、版本、内容哈希、批准和发布状态 |
| `discourse_publish_draft` | 按账号发布已经准备好的具体版本 |

除账号列表外，所有工具都必须传 `account`。没有全局“当前账号”，也没有通用 HTTP 请求、Key 导出、跨用户代发、删帖或管理员工具。主题读取只返回当前首批帖子；更多正文可以按帖子 ID 调用单帖读取。论坛内容均为不可信资料，不应作为对 Agent 的操作指令。

## 草稿与发布

创建主题的工具参数示例，只是本地草稿：

```json
{
  "account": "audit",
  "kind": "create",
  "title": "已发布版本安全修复公告",
  "category_id": 60,
  "raw": "经人工核对后填写修复内容、严重性和首个正式修复版本。"
}
```

回复使用 `kind: "reply"`、`topic_id`、`raw`；编辑使用 `kind: "edit"`、`post_id`、`raw`，只允许修改配置账号自己的帖子。编辑不改变主题标题。修订已有草稿时传 `draft_id` 和当前 `expected_revision`，每次修订都会使批准失效。

默认审核过程：

1. Agent 准备草稿，返回 `id`、`revision`、`hash` 和完整正文。
2. 在本地用 `draft-show` 查看账号、站点对应配置、目标分区和正文，人工确认后批准指定版本及哈希。
3. Agent 调用 `discourse_publish_draft`，传 `account`、`draft_id`、`revision` 和唯一 `request_id`（8–128 位字母、数字、下划线或连字符）。

```powershell
& $taskPython -m discourse_mcp draft-show audit DRAFT_ID
& $taskPython -m discourse_mcp draft-approve audit DRAFT_ID --revision 1 --hash FULL_HASH_FROM_DRAFT
```

同一个 `request_id` 再调用只返回原有结果；相同草稿版本换一个 request_id 也不能再次发送。SQLite 事务在多个客户端进程之间争抢唯一发布权，修订使用版本检查防止本地异步覆盖。

状态含义：

| 状态 | 处理 |
| --- | --- |
| `draft` | 尚未提交，可修订；默认要求本地批准 |
| `in_flight` | 已取得发布权，可能仍在请求，也可能进程中断；不能重发 |
| `published` | 已取得有效论坛回执，保存帖子链接 |
| `rejected` | 明确的 HTTP 4xx 拒绝或操作者确认未发送；修订后重新审核 |
| `unknown` | 超时、连接中断、5xx、审核排队或回执异常；先核对论坛，禁止自动重试 |

不自动重试写请求、不跟随重定向、不把上游错误正文传回模型。Discourse 审核排队响应也不会被误当成已发布。应先核对审核队列和主题，而不是创建另一份草稿绕过未知状态。

**异常恢复必须先停止所有相关 MCP 客户端**，确认没有写请求仍在执行；服务无法从已退出的本地进程判断远端请求是否最终完成。核对论坛后：

```powershell
# 确认已经发布：服务会读取该帖子，核对作者、正文、分区和目标。
& $taskPython -m discourse_mcp draft-resolve audit DRAFT_ID --post-id POST_ID --clients-stopped

# 人工确认远端完全没有写入或待审核请求，之后仍需修订、重新批准。
& $taskPython -m discourse_mcp draft-resolve audit DRAFT_ID --confirm-not-sent --clients-stopped
```

这是保守的本地去重与人工核对机制，不能承诺远端恰好执行一次。操作者仍可新建内容相同的独立草稿，服务不会按正文哈希阻止正常重复内容。

## 安全公告的使用约定

保持已有披露边界：先私下整理和通知，只有正式发布版本已修复、经人工复核的项目才进入公开公告。公告仅写修复内容、漏洞严重性和首个正式修复版本；不附载荷、复现步骤、未修复问题或攻击链。分支提交、PR 合并和本地测试都不能代替正式发布证据。

本地批准绑定正文、目标账号、账号配置版本和草稿版本。它是人工审核关口，不是自动证明漏洞成立、严重性准确或修复已正式发布的程序。MCP 不提供自我批准工具。

## 安全边界与限制

- Key 使用当前用户范围的 Windows DPAPI，并用账号别名绑定额外熵；没有明文存储回退。能够以同一 Windows 用户执行任意代码的程序仍能调用 DPAPI；它不是同一用户下对恶意 Agent 的 OS 隔离。客户端别名限制也不能约束已经拥有任意本地 shell 权限的程序。
- 生产站点仅接受 HTTPS 根域地址，不支持子路径安装。TLS 使用系统证书校验。当前实现不继承环境 HTTP 代理配置，不支持代理必需的网络。
- 本地限制分区、动作和作者；Discourse 本身还应配置对应账号与 Single User Key 的最小权限。不要假设论坛的 API scope 自带所有分区隔离能力。
- 编辑会在发送前再次核对远端版本及原文哈希，拒绝已知的远端修改。但所用 Discourse 编辑 API 没有本服务可依赖的原子条件写保障，核对与提交之间仍存在远端并发窗口；分区移动也有类似窗口。不要让多个写入者同时编辑同一帖子。
- 不提供远程 HTTP MCP、Windows 常驻服务、上传附件、搜索全站、用户管理、删帖或跨账号审核权限；需要时再按实际范围扩展。

## 测试与接口来源

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

测试使用临时目录、合成 Key 和 `127.0.0.1` 模拟论坛，不读取历史凭据、不访问真实论坛、不测试审计对象。DPAPI 测试需要正常 Windows 用户会话；受限沙箱可能因为没有用户配置/凭据上下文而失败，此时应在普通本地终端运行，不能改用明文。

本地验证记录（2026-10-21）：23 项测试通过，覆盖 DPAPI 密文/篡改检查、账号和分区隔离、动作权限、草稿版本/批准失效、密钥轮换、跨进程发布争抢、重复请求、重定向、未知结果恢复及 MCP stdio 新旧握手。`pip check` 通过。真实账号尚未录入，真实论坛权限、API Key scope、发帖和审核队列行为尚未验收；没有发出真实论坛写请求。

实现核对了 [Discourse 官方 OpenAPI](https://github.com/discourse/discourse_api_docs/blob/main/openapi.json) 的 `POST /posts.json`、`GET /t/{id}.json`、`GET/PUT /posts/{id}.json`；MCP 使用[官方 Python SDK](https://py.sdk.modelcontextprotocol.io/)，凭据范围参考 [Microsoft CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。这些接口契约和本地模拟测试不代表真实论坛已验收。
