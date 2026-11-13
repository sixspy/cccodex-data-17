# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 跨日账本与开跑门闩（`/ledger/`）

在采集任务之外新增一套跨日账本，**页眉、历史日册、清账任务、今日开跑四处联动**。

- **后台日期可钉死**：`app/services/clock.py` 读 `TEST_BACKOFFICE_DATE`（测试用），生产取 `date.today()`。
- **页眉**：并排显示最近若干天（默认 7）的跨日未了条数（按后台日期、最旧在前）、今日已收、当前清账口径，所有页面可见。
- **日册**：今日新收只写今日账册（`entry_kind='new'`），绝不写进历史日册；历史日册题名与条目只读，切换天只能翻看。跨日点「跨日封账」把在途日册转历史并把条目挂为未了（`pending_items`，带来源日期 `source_day`）。
- **清账顺序门闩**：必须从最旧的一天清到昨天，不许跳日；有更旧未了时先清昨天会失败，清账任务卡写「跳日」。
- **清账口径（选定写死）**：
  - `merge_today`：并入今日账册，条目保留来源日期字段（`entry_kind='merged'`，`source_day` 不变）。
  - `separate_task`：每日生成独立清账任务写入独立清账册，今日账册保持空白直到清完。
  - 口径一旦锁定不能改口；发起清账方式与页眉写死的不一致，当场拦住，卡片写「方式不对」。
- **开跑门闩**：任一更早日期的未了未清，今日开跑失败写「跳日」；某日清账任务 running 时，既不能开另一日清账，也不能今日新收（写「清账未完成」）。每次尝试落 `run_attempts`。
- **并发**：两人同刻点不同日期清账，只许最旧一天成功，其余失败写「顺序锁」。进程内门闩见 `app/services/settlement_gate.py`（基于并发组目标日期裁决，与线程调度无关），库内 `BEGIN IMMEDIATE` + running 状态做跨请求兜底。

验收测试：`tests/test_ledger_gate.py`（七段）+ `tests/test_ledger_concurrency.py`（真并发）。

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
