# codegraph

自研代码图谱：把 **Python 仓库解析成调用图**，用 **KuzuDB** 落库，再通过 **Deep Agents** 暴露架构感知的 `impact` / `context` / `query` 工具。

> 思路借鉴开源项目 [GitNexus](https://github.com/abhigyanpatwari/GitNexus)（其许可证为 PolyForm 非商用，故自行实现，绕开许可问题）。核心借鉴三点：**Tree-sitter 解析 + 语言无关中间结构**、**混合图存储（节点表 + 单一关系表语义）**、**impact 爆炸半径（上游/下游 + 置信度）**。

## 架构

```
代码仓库 (Git)
   │  Tree-sitter AST（parse_python）
   ▼
解析中间结构（Symbol / Import / CallSite）     ← parser.py
   │  作用域内调用解析（精度优先，宁可漏连不瞎连）
   ▼
内存图（节点 + CALLS/CONTAINS/HAS_METHOD/IMPORTS） ← graph.py
   │  混合 schema：File/Function/Class 节点表 + 强类型关系表（逻辑单一 CodeRelation）
   ▼
KuzuDB 图谱（本地 .db 文件）                   ← db.py
   │  find_callers / find_callees（逐层 BFS）+ 增量 helpers（MERGE/DETACH DELETE）
   ▼
增量写回（sha1 快照 → importer/CALLS 反向传播 → 子图更新） ← incremental.py
   ▼
语义检索引擎（BM25 关键词 + 本地向量 + RRF 融合）   ← retrieval.py + embedding.py
   ▼
impact 爆炸半径报告（JSON）                    ← impact.py
   │  Deep Agents @tool 注册
   ▼
架构感知 Agent（build_agent）                 ← tools.py
```

## 安装

```bash
cd codegraph
uv sync            # 安装全部依赖（含 deepagents + langchain-openai 示例 provider）
```

## 快速使用

```bash
# 1) 首次建图入库（写入快照 cg.db.snapshot.json）
uv run python -m codegraph.cli analyze examples/sample_repo --db cg.db

# 2) 改代码后再 analyze → 自动增量写回（只重解析受影响文件）
#    —— 改了多少、importer 传播到哪些，会打印出来
uv run python -m codegraph.cli analyze examples/sample_repo --db cg.db

# 强制全量重建（忽略快照）
uv run python -m codegraph.cli analyze examples/sample_repo --db cg.db --force

# 3) 查某符号的爆炸半径
uv run python -m codegraph.cli impact auth.login --db cg.db --depth 3

# 4) 查某符号的上游/下游
uv run python -m codegraph.cli callers auth.login --db cg.db --depth 3
uv run python -m codegraph.cli callees auth.login --db cg.db --depth 3

# 5) 语义检索（M4）：按关键词或意思（支持中文）找代码符号
#    --repo 与 --db 必须指向同一仓库（索引取代码片段）
uv run python -m codegraph.cli search "做登录鉴权的函数" --db cg.db --repo examples/sample_repo --k 5
```

# 6) Web 可视化界面：浏览器里看图谱、搜符号、查爆炸半径
uv run python -m codegraph.server --db cg.db --repo examples/sample_repo --port 8000
# 打开 http://localhost:8000/

### Web 可视化界面

内置一个零依赖（标准库 `http.server`）的浏览器界面，复用 CLI 同一套 db / impact / retrieval 查询：

- **图谱可视化**：文件 / 类 / 函数节点 + CALLS / CONTAINS / HAS_METHOD / IMPORTS 边（vis-network，CDN 加载；断网则降级为文字提示）
- **统计卡片**：文件 / 函数 / 类 / 边数量、语言分布（按扩展名：Python / JavaScript / TypeScript / Java）
- **符号查找**：输入片段定位符号并跳转图谱
- **选中节点**：查看该符号的上游 / 下游调用 + 爆炸半径（impact）
- **语义检索**：同 M4 的 BM25 + 向量混合检索（首次触发会建索引 + 加载 embedding 模型，较慢）

API（JSON）：`/api/stats`、`/api/graph?limit=`、`/api/symbols?q=`、`/api/impact?symbol=&depth=`、`/api/callers|callees?symbol=&depth=`、`/api/search?q=&k=`。

### 语义检索（M4）

**检索引擎本身没有大模型**——是本地、确定、免费的计算：BM25 关键词 + 本地 embedding 向量（多语言模型，首次用自动下载 ~470MB 到本地缓存），再用 RRF 融合排序。LLM 在最外层、可选（见 tools.py 的 `semantic_search` Agent 工具）。

- 中文 query 能对齐英文代码语义（如「做登录鉴权的函数」→ `auth.login` / `verify_password`）
- 纯中文 query 无 BM25 命中（中文不参与关键词切词），全靠语义向量；中英混合 query 才两者融合

### 增量写回（M2）

`analyze` 无快照时全量建图并落快照；之后每次运行按 **sha1 内容哈希** 判定变更，并做三件事：

1. **变更判定**：对比快照，得出 changed / added / deleted。
2. **受影响面传播（GitNexus 写集闭包）**：迭代找出变更文件的
   - **importer**（沿 IMPORTS 反向：谁 import 变更文件）
   - **符号调用者**（沿 CALLS 反向：谁调用了变更文件的符号）
   直到收敛——删掉变更文件符号时，所有指向它们的调用边都会被级联删除，这些源文件必须重解析。
3. **子图写回**：只对受影响文件 `DETACH DELETE` 旧节点/边 + `MERGE` 新子图，未变文件的行不动。

保证：**增量结果 ≡ 重新全量建图的结果**（黄金正确性，见验证）。

### Deep Agents 工具层

```python
from codegraph.tools import build_agent

agent = build_agent(
    model="openai:gpt-4o",       # 或任意 LangChain 模型实例；按需换 provider
    db_path="cg.db",
)
await agent.run("把 auth.login 改名，先告诉我影响范围")
```

`build_agent` 注册三个工具：
- `impact(symbol, depth)` — 改动该符号会波及哪些调用者/被调用者（爆炸半径）
- `context(symbol)` — 某符号的直接上游/下游
- `query(text)` — 按名字片段搜索符号

## 验证

```bash
uv run python scripts/step1_validate.py   # 解析器：符号/import/调用提取（含相对导入绝对化）
uv run python scripts/step2_validate.py   # 建图 + KuzuDB 落库 + 调用链
uv run python scripts/step4_validate.py   # Deep Agents 工具注册 + agent 构造
uv run python scripts/step5_validate.py   # 增量写回基础（importer 传播 / up-to-date / 删除清边）
uv run python scripts/step6_validate.py   # 业务工程级增量（6 轮演进 × 增量≡全量等价）
uv run python scripts/step7_real_validate.py  # 真实 GitHub 项目增量（itsdangerous/flask/fastapi）
uv run python scripts/step8_validate.py   # 语义检索框架 + BM25 关键词召回
uv run python scripts/step9_validate.py   # 本地向量 + 混合检索（中文 query 召回英文代码，RRF 融合）
uv run python scripts/step10_validate.py  # 多语言端到端（py+js+ts+java 混合仓库，分发/模块/调用/落库）
uv run python scripts/step11_real_validate.py  # 真实多语言项目（zod TS + gson Java）+ 并行解析加速
```

**真实项目验证结果**（改核心文件 + 新增模块，均验证「增量库 ≡ 全量库」；批量落库优化后）：

| 项目 | 体量 | 全量 | 增量 | 重解析 |
|---|---|---|---|---|
| itsdangerous | 2352 行 / 8 文件 | 0.29s | 0.35s | 5/8 文件 |
| flask | 19374 行 / 24 文件 | 0.26s | 0.55s | 21/24 文件（改 app.py 传播面大） |
| fastapi | 44318 行 / 52 文件 | 0.46s | 0.68s | 12/52 文件 |

> **性能优化与诚实结论**：剖析发现解析不是瓶颈（fastapi 52 文件解析仅 0.16s），真正瓶颈是逐条 SQL 落库（1600+ 次 execute）。改成 **UNWIND 批量写回**后落库提速约 13×（fastapi 全量 1.58s → 0.46s）。副作用：全量足够便宜后，对 <5 万行仓库增量相对全量的速度优势倒挂（增量固定开销 = 快照对比 + 引用方 BFS + 子图写回 + CHECKPOINT）。**增量仍对超大仓库/频繁小改有核心价值**（全量分钟级时只重解析受影响子图）。

## 目录

```
codegraph/
├── codegraph/
│   ├── parser.py   # Tree-sitter 多语言：Python/JS/TS/Java 符号/import/调用提取 + 并行分块解析
│   ├── graph.py    # 调用解析 + 建图（精度优先；支持增量 extra 索引/文件注入）
│   ├── db.py       # KuzuDB 混合 schema + BFS 查询 + 增量 helpers（UNWIND 批量写回/MERGE/DETACH DELETE）
│   ├── incremental.py  # 增量写回：sha1 快照 + 写集闭包传播 + 子图更新（多语言文件发现）
│   ├── retrieval.py    # 语义检索：BM25 索引 + 可插拔语义后端 + RRF 融合
│   ├── embedding.py    # 本地向量后端（fastembed/onnx 多语言模型，中文 query 对齐英文代码）
│   ├── impact.py   # 爆炸半径报告（上游/下游，按深度分组）
│   ├── tools.py    # Deep Agents 工具层（impact/context/query/semantic_search，惰性 import）
│   └── cli.py      # analyze(自动增量/--force) / impact / callers / callees / search
├── examples/sample_repo/   # 示例 Python 仓库（auth/service/models/main）
└── scripts/                # 分步验证脚本（step1~step11）
```

## 当前边界（V1，继承自 GitNexus 的同款取舍）

- **精度优先**：未解析成功的调用直接丢弃并计数，绝不靠猜测硬连（对齐 GitNexus 的 "漏报是 coverage 限制，编造是谎言"）。真实项目（itsdangerous）未解析 96 处即为此类 coverage 限制。
- **静态解析局限**：动态调用（`getattr`/`eval`/反射）、实例方法调用（`svc.create_user`）覆盖率不全——本版靠"全库唯一短名"兜底，靠 `unresolved` 计数暴露缺失。
- **重复调用边**：同一函数内多次调用同一符号会生成多条 CALLS 边（各调用点一条），KuzuDB 的 `count(*)` 对此类重复 rel 计数不可靠，等价性验证用「数行 + 去重」双指标。
- **语义检索召回是"意思相近"而非精确**：本地多语言 embedding 召回按语义相似度排序，仓库若无精确对应（如 fastapi 无 login 函数）会召回最接近的鉴权/授权符号。
- **多语言（已支持）**：Python / JS / TS（含 tsx）/ Java，按扩展名分发到各语言 tree-sitter 解析器，统一产出 `ParsedFile`（借鉴 GitNexus language-provider 注册表模式）。
- **Java V1 简化**：模块命名用文件路径（含文件名）而非源码 package，故类 FQN 形如 `com.google.gson.Gson.Gson`；改用 package 需把 package 持久化到库并同步增量索引（后续优化）。跨包调用已靠全库唯一短名兜底可靠连上。
- **多线程分块解析（已实现）**：进程池并行（规避 GIL 解析串行），分块借鉴 GitNexus worker-pool 的"文件数 + 字节上限"双约束（SUB_BATCH_SIZE=1500 / 8MiB）。zod 517 文件解析 0.95s→0.56s（1.73×）。
