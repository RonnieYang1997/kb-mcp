# kb-mcp

本地知识库检索服务（MCP over stdio）。给「独夫之心」语音转录语料做**只读**索引，
提供中文混合检索（FTS5 trigram 全文 + 本地 ONNX 向量，RRF 融合），
供 AI 做检索、整理、总结。

> 设计上对齐（并修掉）了原先 KMCP 的 9 个硬伤：中文切块、中文/英文模型混用、
> 容器无外网导致的静默失败、删源永远失败产生孤儿文档、索引过期等。

---

## 一、结构与位置

| 项目 | 路径 |
|---|---|
| 代码（本仓库） | `C:\Users\Ronnie\Documents\GitHub\kb-mcp` |
| 索引数据库（**库外**） | `%LOCALAPPDATA%\kb-mcp\index.db` |
| 日志（**库外**） | `%LOCALAPPDATA%\kb-mcp\logs\` |
| 模型 | `<仓库>\models\bge-small-zh-v1.5\`（不进 git） |
| 虚拟环境 | `<仓库>\.venv\`（不进 git） |
| 资料库（**只读**） | `C:\Users\Ronnie\Documents\GitHub\dufuzhixin` |
| 整理/总结产物 | `...\conversations\aionrs-temp-5c938b2a\kb-mcp-产出\` |

### 只读保证

- 对资料库只有 `os.walk` / `os.stat` / `open(path, "rb")`，代码里**不存在**向源库写入的分支；
- 每次索引会记录源库 `.git/HEAD`（纯文件读取，不调 git），前后不一致会告警并返回非零退出码；
- `kb doctor` 会静态扫描全部代码，列出每一处写盘调用及其理由（扫描器在 `kb/audit.py`）。
  唯一允许的写入目标是**索引库目录与日志目录**，都在源库之外。

---

## 二、常用命令

```powershell
$kb = "C:\Users\Ronnie\Documents\GitHub\kb-mcp\.venv\Scripts\python.exe"
$repo = "C:\Users\Ronnie\Documents\GitHub\kb-mcp"

& $kb -m kb doctor                 # 环境、源库、模型、只读自审
& $kb -m kb sources                # 列出资料库（加库口子见 add-source）
& $kb -m kb verify                 # 只读体检：统计 BOM/乱码/失效正文残留
& $kb -m kb index                  # 增量索引（改了配置或首建库时用 --full）
& $kb -m kb index --fts-only       # 只建全文（约 1 分钟），向量留待后台补齐
& $kb -m kb search "叙利亚局势" --top-k 5
& $kb -m kb verify-quote "第一代下水道是德国修的"   # 引文核对（别名 vq）
& $kb -m kb verify-article 某篇文稿.md --strict     # 整篇核对（别名 va）
& $kb -m kb stats                  # 数量、是否过期、最近任务
& $kb -m kb add-source --root "D:\某个库" --id mylib --label "我的库"
```

MCP 服务由 AionUi 以 stdio 方式拉起，入口是 `bin\kb_serve.py`，
注册名 **`kb-mcp`**，命令：

```
C:\Users\Ronnie\Documents\GitHub\kb-mcp\.venv\Scripts\python.exe
C:\Users\Ronnie\Documents\GitHub\kb-mcp\bin\kb_serve.py
```

### 六个工具

| 工具 | 用途 |
|---|---|
| `search` | 混合检索，返回带出处的片段（标题/日期/BVID/路径/片段序号/余弦）。`sort=relevance`（默认）/ `sort=recent`（按出片时间倒序） |
| `fetch` | 按 `doc_id`/路径/BVID 取完整正文（已清洗），支持 `offset`+`max_chars` 分段 |
| **`verify_quotes`** | **引用前必做**：把要引用的句子拿回**只读源文件原文**逐字核对（见下节） |
| `list_documents` | 按日期、标题、BVID、资料库枚举文档，用于挑整理清单 |
| `stats` | 索引状态、是否过期、模型状态、最近索引任务 |
| `reindex` | 增量刷新；`full=true` 走后台全量（返回 job id，用 `stats` 查进度） |

### 引文核对：防止「假引文」

`search`/`fetch` 返回的是**清洗过、切块过**的文本。它能给你线索，但**不能直接当引文**：

- 切块会把长句截断——跨片段的一句话，在任何单个片段里都不完整；
- 清洗会丢掉乱码表头——「索引里没有」不等于「原文里没有」；
- 更常见的是人（或 AI）顺手把引文「抚平」一下：加标点、调语序、换词、插一句括注解释。
  结果是引文读起来通顺，却和原文不再是同一句话。

所以 `verify_quotes` 的分工是：**用 FTS 快速定位候选（便宜），判定一律回到源文件原文（准）**。

```
kb verify-quote "第一代下水道是德国修的" --bvid BV1GK411v7w9   # 单条（别名 kb vq）
kb verify-quote "引文1" "引文2" --json                        # 多条
kb verify-article 文稿.md                                     # 整篇（别名 kb va）
kb verify-article 文稿.md --strict                            # 有引文没过则退出码 5
```

判定档位（越靠前越干净）：

| 状态 | 含义 | 能否引用 |
|---|---|---|
| `verbatim` | 逐字一致 | ✅ 可放心引用 |
| `whitespace` | 仅空白/全半角差异，文字一字不差 | ✅ |
| `loose` | 仅标点差异（转录标点本身就不统一） | ✅ 文字一字不差 |
| `annotated` | 文字一致，但引文里插了原文没有的括注 `（…）` | ⚠️ 逐条列出，建议改 `［］` 或删掉 |
| `modified` | 多字/少字/改字（例：原文「60年代以后西方就没有…」被写成「西方60年代以后就没有…」） | ❌ 附 `matched_text` 与多出/缺少的片段 |
| `other_doc` | 这句是真的，但不在你标注的那一篇里 | ❌ 出处标错了 |
| `not_in_body` | 只在乱码表头/元数据区，正文里没有 | ❌ 不能引 |
| `not_found` | 全库查不到 | ❌ 附最接近的原文片段供改写 |
| `error` | 空引文、BVID/doc_id 不存在 | ❌ |

几个已处理的细节：引文里的省略号（`……`/`...`/`<>`）会**自动拆段分别核对**；
Markdown 加粗标记会被剥掉；繁体转录必须照原文保留繁体（如 `如果說中國救災不利 請舉例`）；
结果里的 `in_chunk` 标志专门暴露「跨片段」这种索引侧假阴性。

**实测效果**：本仓库第一篇产出文稿（16 条引文）首轮核对 **13 条里 8 条没通过**——
全是「顺手抚平」造成的：调语序、丢字、插括注、把转录的错别字改成了正确写法。
逐条照原文改写后全部通过（1 逐字 + 12 仅标点差异）。

---

## 三、索引怎么保持新鲜

> **第二步安全开关**：`config.json` 里 `scan.auto_index` 是总闸。
> 为 `false` 时，上面三条路径（事件驱动 / 惰性兜底 / 手动 `reindex`）**全部立即返回锁定态**，
> 连源库目录都不遍历、不建片段、不算向量（`kb index` 退出码 `4`，`reindex` 返回 `"locked": true`）。
> 只有用户确认资料修复完毕、把该值改为 `true`（见第五节第 0 步），才会真正读取源库。

三层，缺一不可：

1. **事件驱动**：`daily_scan.py` 尾部调用 `bin/kb_index.py`（增量）。
   该脚本在扫描并镜像到 GitHub 仓库之后触发，因此此刻仓库已是最新。
2. **惰性兜底**：`search`/`list_documents`/`stats` 会先做陈旧检查
   （逐文件比对源库 `mtime`/`size` 与索引库记录），发现更新就地补索引。
   为控制开销，检查频率受 `scan.stale_check_seconds`（默认 300 秒）节流。
3. **手动**：随时 `reindex`。

> 注意：`daily_scan.py` 写的是主库，再由它自己镜像到 GitHub 仓库；
> 索引盯的是**仓库**，所以两者之间不会出现「索引到一半的新文件」。

---

## 四、性能实测（本机 i5-10210U / 4 核）

| 项目 | 实测 |
|---|---|
| 语料规模 | 3684 篇 / 1522 万字 → **57,114 个片段**（350/80 切块） |
| 解码+清洗+切块（全文索引） | 全库约 **1 分钟** |
| 向量化 fp32 | 227 ms/片段 → 16k 片段/小时 → 全量 **约 3.6 小时** |
| 向量化 int8（默认） | 147 ms/片段 → 24k 片段/小时 → 全量 **约 2.3 小时** |
| 加线程 | 4→8 线程无提升（已接近该 CPU fp32/int8 算力上限） |

**int8 的质量代价（实测 120 块 / 12 条查询）**：与 fp32 向量平均余弦 **0.984**，
Top-1 检索结果 **100% 一致**，Top-5 平均重合 **86.7%**。
故默认用 int8；若要换回 fp32，改 `config.json`：

```json
"embed": { "model_file": "onnx/model.onnx" }
```

查询侧开销：单次混合检索约 **60–150 ms**（含陈旧检查；节流命中时更快）。

**首次建库实测（2026-09-24，3684 篇）**：全文索引 48 s → 57,114 片段；
后台全量向量化 `full-20260924-0902` 用时 **8784.8 s（2 h 26 m）**，产出 57,114 条向量，
与上表 int8 预估（2.3 h）吻合。建库全程源库 3684 个文件的 sha1 / size / mtime 与 `.git/HEAD` 均未变。
此后每天 21:00 的 `daily_scan` 增量调用只需**数秒**（例：新增 3 篇 14 个片段，2.4 s）。

---

## 五、第二步（首次建库）步骤

> 前提：确认资料修复完毕。

```powershell
# 0) 解锁索引（把 scan.auto_index 改成 true；这是唯一的放行开关）
#    编辑 config.json： "scan": { "auto_index": true, ... }
& $kb -m kb doctor          # 应打印 [索引开关] auto_index=true

# 1) 先让全文检索可用（约 1 分钟）
& $kb -m kb index --fts-only

# 2) 后台补齐向量（约 2.3 小时，可放着跑）
& $kb bin\kb_index.py --full --job-id manual1     # 或直接： & $kb -m kb index --full
```

`--full` 会重建全部片段与向量；增量（不带 `--full`）只处理新增/改动/删除。

---

## 六、目录说明

```
bin/        入口脚本（kb_serve.py 供 AionUi 拉起；kb_index.py 供 daily_scan 调用）
kb/         实现：config / textproc / store / embed / search / indexer / server / cli / audit
scripts/    fetch_model.py：从 hf-mirror 或 ModelScope 拉取 ONNX 模型并校验
kb/         config.py 配置 · store.py SQLite/FTS5 · textproc.py 清洗切块 · embed.py ONNX · indexer.py 增量索引（含安全锁）· search.py 混合检索 · verify.py 引文核对 · server.py MCP · cli.py 命令行 · audit.py 只读自审 · console.py 输出编码
tools/      selftest.py 端到端自测 · health_check.py 索引体检 · bench_embed.py 性能基准 · compare_models.py 模型对比 · estimate.py 规模估算
            estimate.py 规模预估 · compare_models.py 模型取舍对比
config.example.json  默认配置（含源库定义，提交进 git）
config.json          本机配置（绝对路径，不进 git）
```

---

## 七、已验证

`python tools/selftest.py` → **59/59 PASS**，含：

- 清洗：BOM、GBK 乱码表头、乱码小标题在索引里彻底消失，正文完整保留；
- 失效正文识别：JSON `upstream_error` / HTML 5xx → 标记 `dead` 且不产生片段；
- 检索：全文命中、向量命中、RRF 分数降序、混合结果两个来源都有贡献；
- 同源折叠：同一篇最多保留 `search.max_per_doc` 个片段；`sort=recent` 按出片时间倒序；
  空查询/纯标点返回空结果而不是随机片段；
- `fetch` 取全文；陈旧检查能发现改动并自动补索引、节流生效；
- **第二步安全开关**：`auto_index=false` 时 `ensure_fresh` 与 `index_source` 均直接返回锁定，
  不遍历源库、不建索引、不算向量、文档数不变；
- **引文核对**：原文原句 → `verbatim`；改一个字 → 判不通过并给出原文；
  作者插的括注 → `annotated` 且逐条列出；空引文/假 BVID/假 doc_id → `error` 不崩；
  省略号引文自动拆段；**跨片段长句能被核对到（`in_chunk=false`）**——证明核对必须回原文；
- **源库三重证据未变**：源文件 sha1、mtime、`.git/HEAD` 全部未变；
- **MCP 协议**：initialize 握手、tools/list 返回 6 个工具、六个工具调用全部成功、
  未知工具返回 JSON-RPC 错误不崩溃、exit 正常退出、stdout 无非协议污染。

## 八、健壮性：几处"看起来没事、其实会咬人"的地方

| 场景 | 行为 |
|---|---|
| 后台全量任务跑到一半被杀/断电 | 下次索引自动**补上缺向量的片段**（片段落盘与向量写入是两个事务，本来会留下"搜得到、向量搜不到"的空洞） |
| 换了 `embed.model_file`（int8 ↔ fp32）却没重建 | 检测到向量模型标识变化 → **只更新全文、拒绝混写向量**，并在 `index_log` 记警告；需 `--full` 重建 |
| 索引库里混了两种维度/模型的向量 | `load_vectors` 只取占多数的那一种（按 blob 长度盲目 reshape 会**静默算错**相似度） |
| 被强杀的任务留下 `running` 状态 | 依据「工作进程 pid 是否存活 + 15 分钟心跳」自动标记 `interrupted`，不会永久挡住后续索引 |
| `reindex` 撞上正在跑的任务 | 返回「已有任务」+ 进度，不抢写锁；`force=true` 才强排队 |
| `full: "false"` / `"no"` / `0` | 严格布尔转换 → **不会**误触发数小时的全量重建 |
| 空查询 / 纯标点查询 | 返回空结果 + 说明，而不是拿空串去算向量得到一堆"看着像结果"的随机片段 |
| 引文被"抚平"（调语序、丢字、插括注、顺手改错别字） | `verify_quotes` 逐条报出差异；`not_found` 时给出最接近的原文片段，`other_doc` 时指出出处标错了。**别信"读起来很像"的引文** |
| 引文跨了片段边界 | 只查索引必然假阴性（片段是 350 字窗口切出来的），所以判定回到源文件；结果里 `in_chunk=false` 会把这种情况标出来 |
| 引文里带省略号 | 不能整句核对 → 自动按 `……` 拆段，每段单独验，避免"省略号里夹带私货" |
| Windows 下把输出重定向到文件/管道 | 只在"非终端"时把 stdout 钉成 UTF-8，避免 cp936 造成乱码甚至 `UnicodeEncodeError` 崩溃 |
| 一篇长对话的相邻片段挤满结果 | 默认同一篇**最多保留 2 个片段**（`search.max_per_doc`），腾出位置给别篇；被折叠的条数会写在 `notes` 里 |
| 想问"最近讲了什么" | `search(sort="recent")` 按出片时间倒序，新内容不会被历史高分篇目盖住 |
| `top_k: "abc"`、未知 `mode`、未知 `source` | 参数容错（钳位/回退 hybrid/明确报错），不把 Python 异常泄漏给调用方 |

`python tools/health_check.py` 是只读体检（11 项）：片段与全文行数一致、无孤儿全文行、
每个片段都有向量、维度/范数统一、向量只来自一个模型、HEAD 与索引时一致、与源库无差异。