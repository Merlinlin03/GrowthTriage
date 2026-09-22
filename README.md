# GrowthTriage

## 当前架构：LangGraph＋六个业务 Agent＋Harness

上传数据且配置模型时，依次运行：**用户反馈分析 Agent → 广告指标诊断 Agent → 联合证据分析 Agent → 优化方案规划 Agent → 诊断审核 Agent → 诊断报告 Agent**。反馈与指标承担两个独立分析职责，本版串行执行；没有第七个主 Agent。LangGraph 控制确定性路由，审核不通过时定向退回并重建下游，最多两次；报告生成后由同一审核 Agent 复核，最多修订一次。

每个 Agent 都有独立提示词、Pydantic 契约及真实 DeepSeek 工具调用循环：模型请求 `read_evidence` → Harness 校验本任务证据白名单 → 返回事实/上游产物 → 模型提交分析。工具只有只读权限；指标来自程序公式，模型不修改指标表。工作记忆是本任务工具消息、产物版本和审核意见，不存在跨用户长期记忆。实验卡由规划 Agent 根据证据生成，报告摘要由报告 Agent 生成。

Harness 管理实际请求配额、每 Agent 最多四次模型回合、每 Run 最多 80 次模型请求、分析/报告各 600 秒时限、证据引用检查、风险策略和审批。每日全局请求额度仍由 `GT_LIVE_DAILY_LIMIT` 控制，默认 30；一次正常六 Agent 诊断包含终稿复核，通常至少 14 次模型请求（更多反馈批次/修订会增加），不等于每天能诊断 30 次。数值表确定性计算，叙述数字和因果措辞仅作基础拦截，不能代替人工专业判断。

模型传输异常每次 Agent 执行最多自动重试一次，额外请求照常计入配额；超时不自动重发。报告或终稿复核遇到可重试的模型错误时，页面支持“仅重试报告阶段”：优先复用已有草稿，不重复执行分析或创建沙箱工单；保留原失败审计记录。

基准案例及无 Key 的 local 模式保留为规则演示，不能证明六 Agent 模型已连通。自定义数据有 Key 时使用真实六 Agent 路径，模型失败明确停止。审批状态和 Artifact 存于本地 SQLite；本版没有接入 LangGraph 持久化 checkpointer，待审批通过已提交产物恢复，模型调用中途断电保守失败，不承诺任意节点无损续跑。已批准的沙箱工单即使后续报告审核失败仍保留审计，不回滚为“未执行”。

详细契约见 [六 Agent 实施规格](docs/06-六Agent实施规格.md)。此前的单模型阶段验收是历史记录，不自动视为此版本验收。

海外用户反馈与广告增长异常 Multi-Agent 决策系统。本仓库提供可在 Windows 本机运行的产品：访客隔离 Workspace、基准案例或双 CSV 上传、联合诊断、Harness 风险分级、人工审批、执行轨迹和可下载成果包。它不连接真实广告账户，也不会执行预算、出价、停投或扩量操作。

## 先体验

需要 Python 3.12+、Node.js 20+；首次启动安装依赖需要网络。双击 [启动本地演示.cmd](启动本地演示.cmd)，保留弹出的命令行窗口，再打开 [http://127.0.0.1:8088](http://127.0.0.1:8088)。脚本会补齐缺失的 Python 依赖并构建前端。若 8088 已被旧版服务占用，先关闭旧服务窗口（Ctrl+C），再运行脚本；刷新旧网页不会更新旧后端代码。

日常流程：在工作台点击“新建诊断” → 上传反馈与广告指标两份 CSV → 校验并开始分析 → 必要时处理内部工单审批 → 查看并下载报告。内部工单只保存在本系统，不同步外部系统。广告账户操作仅为建议。

侧栏用途：

- “工作台”展示当前工作区的诊断数量与最近记录；“上传数据”接收已脱敏业务数据。预置案例入口已从产品页面移除，原接口保留用于回归验证。
- “联合诊断”列出当前访客沙箱的 Run，并展示进度、证据和 Trace。
- “审批中心”显示待处理的 R2 决策；没有待审 Run 时显示空状态。
- “成果包”显示已完成的报告；Run 完成前不会有下载项。
- “管理员”是可选的项目所有者控制台；访客体验无需管理员密码。

## 上传自己的数据

在“上传数据”页下载两份 CSV 模板，也可直接试用 [示例反馈](examples/feedback.csv) 与 [示例广告指标](examples/ad_metrics.csv)。反馈与广告指标必须覆盖相邻的 baseline/current 7 天窗口，并使用能对应的市场、平台、Campaign 和 Creative。上传后先质检，再点击“开始分析”；不能只传反馈文件。只使用合成或匿名数据，不上传真实邮箱、电话、密钥或个人身份信息。反馈限 1 MB/500 行，指标限 2 MB/5000 行，UTF-8 编码。原始 CSV 不作为文件保存；规范化记录写入本地 SQLite，访客 Workspace 默认 2 小时后失效并清理。

自定义数据的数字、结论、审批条件来自上传数据，不套用黄金案例。配置 DeepSeek 后，全部已接受的匿名反馈按每批最多 50 条分析，其余 Agent 读取相应事实和上游产物；指标计算、证据门槛和风险策略由确定性代码负责。自动隐私校验无法保证识别全部个人信息，请上传前自行匿名化。配置 Key 的上传路径任一 Agent 失败均明确停止，不生成成功报告；无 Key 的 local 模式使用规则演示。基准案例始终走确定性路径，不用于证明模型连通。

## 可选：DeepSeek 与管理员

本地真实数据运行：复制 `.env.example` 为 `.env.local`，在未跟踪的文件中填写 `DEEPSEEK_API_KEY`，保留 `GT_APP_MODE=production`，然后运行 [启动本地演示.cmd](启动本地演示.cmd)。启动时会检查 Key 是否存在、模型地址是否为 HTTPS；这不等于模型已连通。不要把密钥发在聊天中、写入前端或提交到 Git。默认模型是官方当前示例使用的 `deepseek-flash`，也可在配置文件中明确指定你账户可用的模型 ID。

如只需本地规则回归，在 `.env.local` 中设 `GT_APP_MODE=local` 并留空 Key。密钥仅在本机忽略的配置文件内填写，不写入命令行、聊天或提交记录。已有系统环境变量优先于 `.env.local`；如修改文件后未生效，检查旧环境变量并完全重启服务，不输出其值。

管理员不是普通分析的前置条件。若需要在“管理员”页面点击“检测模型”，先在 `backend` 目录执行 `python -m app.services.admin_auth`，输入至少 12 位密码，将输出的 PBKDF2 哈希配置为 `GT_ADMIN_PASSWORD_HASH`，再启动服务。探针只发送合成文本，返回实际模型、耗时和安全错误码，不返回 Key。不要把明文密码或哈希提交到仓库。

`GT_ADMIN_PASSWORD_HASH` 必须存放生成的哈希，不能填写明文密码；在 `.env` 中用单引号包住完整哈希，避免 Docker Compose 解释其中的 `$`。网页登录时输入原始密码。修改配置后执行 `docker compose up -d` 让容器重新加载配置。

访客是由浏览器 Cookie 标识的隔离 Workspace，不是永久账号：默认有效期 2 小时，每个 Workspace 最多创建 3 个 Run，失败 Run 也占用次数，刷新页面不会重置。管理员使用独立持久 Workspace，当前最多 1000 个 Run，登录 Session 有效期 12 小时；并非无限制。所有角色当前每个 Workspace 最多上传 5 组双 CSV、同时最多 1 个活动 Run，并共享 `GT_LIVE_DAILY_LIMIT`（默认每天 UTC 30 次实际模型请求，包含探针与 Schema 修复请求）。

模型请求默认总等待上限为 60 秒，配置项 `GT_LLM_REQUEST_TIMEOUT_SECONDS` 可设为 10–180；连接等待最多 10 秒。每批最多 50 条，Schema 修复最多一次且另计配额；超时不自动重发。六 Agent 路径使用非思考模式的工具协议和 JSON 输出，不记录模型思维链。此前 8 秒设置曾令单模型 40 条反馈超时，已通过当时版本真实复测修正。

服务启动后，可运行 `python scripts\live_smoke.py`，通过正常的双 CSV 上传、诊断、必要时拒绝 R2 沙箱写入、报告下载路径验证真实模型调用；脚本只用合成示例 CSV，不打印 Key。`GT_LIVE_DAILY_LIMIT` 按实际模型请求数计数（管理员探针每次请求、上传数据每批一次，结构化结果重试也计一次）；大批量上传可能消耗多次配额。
若使用“启动安全本地演示”开启 Secure Cookie，请通过临时公网入口的 HTTPS 地址运行烟测，例如 `python scripts\live_smoke.py https://<当前随机地址>.trycloudflare.com`；默认的 HTTP 本地地址只适用于普通本地启动。

## 临时公网演示

没有服务器也能临时给面试官一个公开网址，但你的电脑和两个命令窗口必须保持在线，网址不永久有效，也没有 SLA。先运行 [启动安全本地演示.cmd](启动安全本地演示.cmd)，确认本地服务可用；再运行 [启动临时公网入口.cmd](启动临时公网入口.cmd)。后者会检查本地服务的 Secure Cookie 配置，通过后由 cloudflared 打印随机 HTTPS 地址。将该地址发给面试官即可使用访客模式。演示结束后在 Tunnel 窗口按 Ctrl+C；只关闭 Tunnel 不会删除本机数据。临时公网未由本仓库自动开启，只有你主动运行脚本才会对外发布。

若通过 Docker 启动，复制 `.env.example` 为 `.env` 并填写 Key；临时公开访问前将 `GT_COOKIE_SECURE=true`。`compose.yaml` 只绑定宿主机 `127.0.0.1:8088`，公网访问仍需单独的 Tunnel。请勿将 `.env`、`.env.local`、数据库或真实反馈数据提交到公开仓库。已提供 `python scripts\public_smoke.py https://<当前随机地址>.trycloudflare.com` 做公开链路烟测，测试沙箱会自动删除。

## 验证与文件

```bat
python -m pip install -r backend\requirements.txt
cd backend
python -m pytest tests -q
cd ..\frontend
npm.cmd run build
```

Docker Desktop 可用时可运行 `docker compose up --build`。自动化测试覆盖黄金指标、上传数据分支、R2 审批与恢复、Workspace 隔离、模型结构化结果、管理员鉴权和 2 槽 Runner。文档依次在 [PRD](docs/01-PRD.md)、[数据实体设计](docs/02-数据实体设计.md)、[概要设计](docs/03-概要设计.md)、[原型评审](docs/04-原型评审记录.md)、[SPEC](SPEC.md) 和 [交付验收](docs/05-交付验收.md)。

无 Key 容器验收使用独立 Compose 和合成数据，端口 18088、独立测试卷，不挂载现有 `data`：

```bat
docker compose -f compose.acceptance.yaml up --build -d
python scripts\container_smoke.py http://127.0.0.1:18088 --restart-container growthtriage-acceptance
docker compose -f compose.acceptance.yaml stop
```

脚本会重启指定验收容器，验证待审批及完成报告持久化，并删除自己创建的测试沙箱。不要把 `--restart-container` 指向正在处理真实数据的容器。此路径固定为空 Key/local 模式，只验收容器与确定性业务闭环，不证明真实模型已连通。前端版本与跨平台依赖锁文件已纳入工程，Docker 使用 `npm ci` 安装。

历史记录（单模型阶段）：已完成第一阶段 Key-to-Live 代码与模拟 Provider 验证，并补充一致性备份、新文件还原、保守启动恢复和请求限制回归。Docker 实际构建、独立 Compose 启动、Linux 33 项测试、完整 HTTP 烟测、重启持久化和容器合成业务数据备份还原均已通过。**2026-09-18 已使用本机真实 Key 完成管理员探针及合成双 CSV Live 验收**：实际模型 deepseek-flash、feedback 为 live、9/9 Step、报告下载成功，使用 2 次模型请求。该结果不代表已验收真实业务数据；真实数据故障演练与独立外网验收仍未完成，不能宣称最终生产级交付。该历史版本仅反馈分类调用 LLM，未使用 LangGraph。当前六 Agent 版本以本文顶部架构及交付验收最新记录为准；仍没有外部工单写入或广告账户执行。

## 本机备份与故障处理

先在本机建立 `backups` 目录，执行（目标必须不存在）：

```bat
python scripts\db_backup.py backup --source data\growthtriage.db --destination backups\snapshot.db
python scripts\db_backup.py restore --source backups\snapshot.db --destination data\restored.db
```

工具使用 SQLite 一致性快照，包含已提交 WAL 数据，校验数据库完整性和外键，不复制 Key 配置。还原只创建新文件；停服后在 `.env.local` 将 `GT_DATABASE_URL` 改为 `sqlite:///D:/shangguigu/Vibecoding_finetune/GrowthTriage/data/restored.db` 再启动。Docker 的数据库地址由 Compose 指定，需要相应修改路径才能切换；不要在运行中覆盖数据库。备份含业务数据及会话哈希，应保留在本机受限目录，勿分享。原访客 TTL 不延长，长期保留结果请下载报告；恢复过期访客数据不会让旧会话重新有效。

重启后：未开始任务可重排队；待审批继续等待；已决定审批仅继续报告，不重复创建工单；已提交报告归并为完成。没有这些确定提交点的 `running` 任务标记 `PROCESS_INTERRUPTED`，不会自动重发可能已计费的模型调用；检查 Trace 后可手动新建诊断。该策略不等于任意节点无损续跑。

API 写请求单进程共享每分钟 120 次额度，超出返回 429/Retry-After；请求体超过 3,100,000 字节返回 413，适用于分块传输。额度重启清零，临时公网访客共享额度，不能代替边缘防攻击措施。自动测试使用空 Key 与临时数据库；Docker、Live 和独立网络验收结果见交付记录。
