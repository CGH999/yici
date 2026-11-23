# 译词 · 设计文档 v0.1

> 状态：**待审**。本文档只定结构与规则，不含实现代码。审完这一版再开工。
> 项目目录：`word-app`（本地开发目录，随机器不同）

---

## 1. 目标与非目标

**这个程序是什么**：单机单人、本地运行的背单词应用。Flask 提供 Web UI，DeepSeek 生成例句并给用户的中文翻译打分，用精简版 FSRS 排复习，`dataset` + SQLite 存数据。

**核心闭环**

```
主页 → 建任务(复制快照) → 预生成 problem(进度页) → 逐词翻译作答
     → AI 两维打分 → 按规则判过关 → 未过关进错误界面 → 循环直到全部完成
     → 询问拼写(可拒) → 结果列表 → 离开
```

**非目标**（本版不做）

- 多账号、登录、权限
- 移动端 App / 原生客户端
- 云端同步、多设备
- 词书市场、在线下载词库
- 发音音频文件（用浏览器 `speechSynthesis`）

---

## 2. 术语

| 术语 | 含义 |
|---|---|
| 容器 container | 能承载单词表的实体，只有两种：**词书**（book）、**词本**（wordbook） |
| 词库 library | `data/library.db`，一个文件，**每张表是一个词书**，表名即词书名 |
| 词本库 wordbooks | `data/wordbooks.db`，结构与词库相同，**表名即词本名** |
| 任务库 tasks | `data/tasks.db`，一个文件，表名 = `<容器名>__learning` / `__review` / `__spelling` |
| problem | 一个词在本轮的任务数据包：`{sentence, translation, model, generated_at}`，JSON 存在任务行里 |
| 一轮 round | 一次「学习」或「复习」从建任务到结果页的全过程 |
| 熟练 | `state='review'` 且 `due - now > proficiency_days`（默认 365 天），**不落字段，实时推导** |

---

## 3. 目录结构

```
word-app/
├─ run.py                       启动入口
├─ requirements.txt
├─ .gitignore                   data/、config/ai.json
├─ docs/
│  ├─ DESIGN.md                 本文档
│  └─ MIGRATION.md              旧词库迁移记录
├─ config/
│  ├─ app.json                  业务配置
│  ├─ fsrs.json                 算法参数
│  ├─ ai.json                   密钥与请求参数（不入库版本控制）
│  ├─ ai.example.json           不含密钥的模板
│  └─ prompts.json              提示词（只读的稳定前缀）
├─ data/
│  ├─ library.db                词库（单文件多表）
│  ├─ wordbooks.db              词本库（单文件多表）
│  ├─ tasks.db                  任务库（单文件多表）
│  ├─ user/history.db           历史
│  └─ memory/deepseek_memory.json   运行时记忆（程序写）
├─ app/
│  ├─ __init__.py               create_app 工厂
│  ├─ config.py                 加载 / 校验 / 热读取
│  ├─ db/
│  │  ├─ manager.py             连接管理（单例，按文件持有连接）
│  │  └─ schema.py              建表 / 补列（幂等）
│  ├─ services/
│  │  ├─ clock.py               今天 23:59:59 等时间边界
│  │  ├─ scheduler.py           D/S 公式（纯函数，无 IO）
│  │  ├─ grading.py             过关判定（纯函数）
│  │  ├─ task_manager.py        任务模块：只做 DB
│  │  ├─ history.py             历史写入与统计
│  │  ├─ ai_client.py           DeepSeek 三个接口
│  │  ├─ ai_memory.py           记忆文件（单写线程）
│  │  ├─ ai_pool.py             并发 + 重试 + 限流 + 计量
│  │  └─ balance.py             余额缓存与警戒线
│  ├─ views/                    home / study / me / dashboard / api
│  ├─ templates/
│  └─ static/
├─ tools/
│  └─ import_wordbank.py        旧 cet4.db → 新库（只取 word/translation）
└─ tests/
   ├─ test_scheduler.py
   └─ test_task_manager.py
```

---

## 4. 数据层

### 4.1 文件布局与连接

| 文件 | 内容 | 连接数 |
|---|---|---|
| `data/library.db` | 每张表 = 一个词书 | 1 |
| `data/wordbooks.db` | 每张表 = 一个词本 | 1 |
| `data/tasks.db` | 每容器 3 张任务表 | 1 |
| `data/user/history.db` | `history` 一张表 | 1 |

启动时对每个文件执行 `PRAGMA journal_mode=WAL`（多连接下减少写锁冲突）和 `PRAGMA foreign_keys=ON`。

**连接策略**：`dataset.Database` 对象**按文件缓存**在 `DBManager` 单例里，**只在请求线程使用**；AI 线程池只发 HTTP，不碰任何 `dataset` 对象。跨库写入（任务库 → 词库 → 历史库）用「三步 + 幂等标记」保证最终一致（见 4.8）。

### 4.2 词表容器结构（词书表 / 词本表同构）

`dataset` 显式声明类型：

```python
def ensure_container_table(db, name):
    t = db.create_table(name, primary_id='id', primary_type=db.types.integer)
    t.create_column('uid',          db.types.text)      # uuid5(word)，区分大小写
    t.create_column('word',         db.types.text)
    t.create_column('translation',  db.types.text)
    t.create_column('phonetic',     db.types.text)      # 可选，空则不显示
    t.create_column('state',        db.types.text)      # new / review
    t.create_column('difficulty',   db.types.float)
    t.create_column('stability',    db.types.float)     # 秒
    t.create_column('last_review',  db.types.float)     # 秒
    t.create_column('due',          db.types.integer)   # 秒
    t.create_column('reps',         db.types.integer)
    t.create_column('lapses',       db.types.integer)
    t.create_column('note',         db.types.text)      # 词级笔记
    t.create_column('favorite',     db.types.boolean)   # 收藏标记
    t.create_column('previewed_at', db.types.integer)   # 最近一次预习时间
    t.create_column('last_answer_id', db.types.text)    # 幂等用，见 4.8
    t.create_column('created_at',   db.types.integer)
    t.create_column('updated_at',   db.types.integer)
    return t
```

字段语义：

- `state`：`new` = 没学过；`review` = 学过、进入复习周期。**没有第三态**，"已熟练"由 `due - now > proficiency_days` 推导。
- `difficulty`（D）/ `stability`（S）：`new` 词的 D/S 为 `NULL`，不是 3.0。
- `uid`：`uuid5(NAMESPACE_URL, "word:" + word.strip())`，**区分大小写**——`China` 与 `china` 是两个不同的词，uid 也不同。同一个词在任何容器里 uid 都相同，因此可以按 uid 跨表去重、把收藏的词加入词本。导入时只跳过**完全相同**的重复（去首尾空白后），不做大小写折叠。
- 索引：`uid`、`state`、`due` 各建索引（`CREATE INDEX IF NOT EXISTS`）。

### 4.3 任务表结构（三张，同构）

```python
def ensure_task_table(tasks, name):     # name = f'{container}__learning'
    t = tasks.create_table(name, primary_id='id', primary_type=tasks.types.integer)
    t.create_column('uid',            tasks.types.text)
    t.create_column('word',           tasks.types.text)
    t.create_column('translation',    tasks.types.text)   # 建任务时的释义快照
    t.create_column('phonetic',       tasks.types.text)
    t.create_column('difficulty',     tasks.types.float)  # 建任务时的 D 快照
    t.create_column('stability',      tasks.types.float)  # 建任务时的 S 快照
    t.create_column('state',          tasks.types.text)   # 建任务时的词库状态快照
    t.create_column('problem',        tasks.types.text)   # JSON，预生成后写入
    t.create_column('problem_state',  tasks.types.text)   # pending / ready / failed
    t.create_column('problem_error',  tasks.types.text)   # 失败原因，UI 上鼠标悬停可见
    t.create_column('task_state',     tasks.types.integer)  # -1 / 0 / 1
    t.create_column('first_pass',     tasks.types.boolean)
    t.create_column('error_count',    tasks.types.integer)
    t.create_column('served_seq',     tasks.types.integer)  # 环形出题顺序，见 5.2
    t.create_column('answer',         tasks.types.text)   # 最近一次作答原文
    t.create_column('sentence_score', tasks.types.float)
    t.create_column('keyword_score',  tasks.types.float)
    t.create_column('note',           tasks.types.text)   # 本轮记的笔记
    t.create_column('applied',        tasks.types.boolean)  # 是否已写回词库，见 4.8
    t.create_column('created_at',     tasks.types.integer)
    t.create_column('updated_at',     tasks.types.integer)
    return t
```

**`spelling` 表**（只做记忆训练，不参与 FSRS，字段更少）：

```python
uid, word, translation, task_state, error_count, first_pass, revealed, created_at, updated_at
```

`revealed`（boolean）表示"当前这个词是否处于答案已展示状态"——这是拼写状态机（5.3）必需的。

### 4.4 历史表

```python
def ensure_history_table(history):
    t = history.create_table('history', primary_id='id', primary_type=history.types.integer)
    t.create_column('answer_id',      history.types.text)   # 幂等键，唯一索引
    t.create_column('uid',            history.types.text)
    t.create_column('container',      history.types.text)   # 词书/词本名
    t.create_column('container_type', history.types.text)   # book / wordbook
    t.create_column('task_type',      history.types.text)   # learning / review / spelling
    t.create_column('time',           history.types.integer)
    t.create_column('first_pass',     history.types.boolean)
    t.create_column('error_count',    history.types.integer)
    t.create_column('sentence_score', history.types.float)
    t.create_column('keyword_score',  history.types.float)
```

`container_type` 必须有：词书和词本可能重名，只靠 `container` 无法区分。

**写入时机**：**每个词完成时写一行**（不是整轮结束时）。所以中途放弃，已完成的词有记录，未完成的无记录。拼写同理。

### 4.5 容器命名规则

实测过（`dataset 2.0.0`）：中文表名、带空格表名、`ALTER TABLE RENAME`、`DROP TABLE` 都正常。

| 规则 | 说明 |
|---|---|
| 长度 | 1–40 字符 |
| 允许 | 中文、英文字母、数字、空格、`-`、`_` |
| 禁止 | `"` `'` `.` `\` `/` `:` `*` `?` `<` `>` `|`、换行、以及**连续两个下划线 `__`**（会撞任务表命名） |
| 唯一性 | **大小写不敏感**（`CET4` 与 `cet4` 是同一张表，SQLite 行为） |
| 禁止前缀 | `sqlite_` |

建书 / 改名 / 删书都要校验；改名 = `ALTER TABLE "旧" RENAME TO "新"`，同时**必须在应用层同步**：`history.container`、`config.app.json` 的 `current_container`、任务库里对应三张表的名字。这一步失败要回滚。

### 4.6 建表 / 补列的幂等流程

```
启动
 ├─ 确保 tasks.db 有 history? 否 → 建
 ├─ 确保 user/history.db 有 history 表
 ├─ 遍历 library.db 里所有非 sqlite_ 前缀的表 → ensure_container_table(补列)
 ├─ 遍历 wordbooks.db 同上
 └─ 遍历 library + wordbooks 的容器 → ensure_task_table 三张
```

`dataset` 的 `create_column` 会比对现有表结构、只补缺失列，天然幂等；已存在的列不会重建、不会丢数据。**注意：给容器加字段时，必须循环遍历所有词书表逐张补**——这是"单文件多表"结构的固定成本，写进 `schema.py` 的职责里。

### 4.7 迁移：旧 cet4.db → 新词库

旧文件 `refuse_to_memorize_words/data/word_banks/cet4.db` **只读、不修改**。

| 旧字段 | 新字段 |
|---|---|
| `word` | `word` |
| `translation` | `translation` |
| 其余全部 | **丢弃** |

- 5833 行，无重复词、无空值、无带空格词（已核对）
- 新表名 `cet4`，建在 `data/library.db`
- 全部 `state='new'`，`difficulty/stability/last_review/due = NULL`，`reps/lapses = 0`
- `uid = uuid5(word)`（原先用的是 `uuid5(word.lower())`，已用 `tools/migrate_uid.py` 迁移）
- `phonetic` 留空（旧库没有音标数据）
- 结果写进 `docs/MIGRATION.md`：导入行数、跳过行数、时间戳

**已知后果**：旧库里 9 个已进入复习周期的词会回到未学状态，历史进度清零。已确认接受。

### 4.8 跨库一致性：三步写入 + 幂等

词库、任务库、历史库是**三个独立文件**，无法放进一个事务。一个词完成时的写入顺序：

```
① 写任务行：task_state=1, applied=0, answer, scores, updated_at
② 计算 D/S/due，写词库（含 last_answer_id = answer_id）
③ 写 history 一行
④ 写任务行：applied=1
```

- 崩溃在 ②之前：`task_state=1 & applied=0` → 启动时重放 ②③④。
- 崩溃在 ②之后、④之前：重放时比对词库的 `last_answer_id`，**相同则跳过 ②**，只补 ③④。
- `answer_id` 用**确定性派生**：`uuid5(命名空间, f"{ctype}|{容器}|{类型}|{任务行 id}")`。
  同一个任务行永远算出同一个 id，所以重放时无需保存中间状态；换一轮任务、重建任务表后
  行 id 变了，id 也就变了，不会和上一轮撞车。
- `history` 表也带 `answer_id` 并建唯一索引，重放时若该 id 已存在就跳过写历史。

这样任何一步中断都不会重复计 D/S 或重复写 history。

### 4.9 时间与"天"的边界

- 所有时间戳一律 **UTC epoch 秒**（`int` 或 `float`），不存字符串。
- 程序内部时区固定 `Asia/Shanghai`（配置项 `app.json.timezone`，默认即可）。
- **复习窗口**：`due <= end_of_today`，其中

```python
end_of_today = int(datetime.now(tz).replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
```

即"今天 23:59:59 之前到期的全部拉进来"。一个词的复习日 = 它 `due` 落在的那一天。

- 学习窗口不受天限制：`state='new'` 按 `id` 升序。

---

## 5. 任务生命周期

### 5.1 一轮的九个阶段

```
① 校验：余额 ≥ 警戒线；同容器同类型无未完成任务（有则询问 继续/丢弃）
② 建任务：按 id 升序取词，复制快照到任务表
        learning: state='new'            ORDER BY id ASC LIMIT N
        review:   state='review' AND due <= end_of_today ORDER BY id ASC LIMIT M
        取到 0 个 → 直接提示，结束
③ 进度页：并发生成 problem（第 7 节）
④ 全部 ready（或用户跳过失败的词）→ 进入学习页
⑤ 循环答题：按**环形顺序**取下一条 `task_state != 1` 的词 → 显示句子 → 用户输入翻译
   （答错的词推到环尾，不会立刻重问，见 5.2）
⑥ 评分 → 更新 task_state / first_pass / error_count
        答对 → 答对页（词义 + 标准译文 + 分数 + 笔记）→「下一题」
        答错 → 错误页（标准译文 + 词义 + 你的答案 + 分数 + 笔记）→「下一题」
        过关且 task_state=1 → 三步写入（4.8）+ 写 history
⑦ 全部 task_state=1 → 询问是否拼写训练
⑧ 拼写（可拒、可中途放弃）→ 每个词完成时写 history
⑨ 结果页：列出本轮每个词的状态（学习中 / 复习中 / 已熟练）
```

### 5.2 状态机（学习 / 复习共用）

| 当前 `task_state` | 答对 | 答错 |
|---|---|---|
| `0`（初始） | → `1` 完成；若这是**本任务行的第一次作答**，`first_pass=True` | → `-1`；`first_pass=False`；`error_count += 1` |
| `-1` | → `0` | 仍为 `-1`（不继续减）；`error_count += 1` |
| `1` | 不再出题 | — |

**出题顺序是一条环，不是栈。** 答错的词**不会立刻重问**：

- 每次作答后，这个词被推到环的尾部（`served_seq = MAX(served_seq)+1`）
- 下一题取环上的下一个：还没出现过的词（`served_seq IS NULL`）按 id 顺序先来，
  出过的词按 `served_seq` 升序排在后面
- 所以答错的词要**绕完一整圈**（其余词都过一遍）才会再出现
- 复习/学习都适用；拼写同样是环，区别只在"答错当场显示答案、当场重输"那一步

补充规则：

- `first_pass` 一旦为 `False` **永久保持**，之后连对也不会变回 `True`。
- 所以错过一次的词，本轮需要**再连续答对两次**才完成（-1→0→1）。
- 「我不会」按钮：直接按答错处理（`-1`，`error_count += 1`），**不调 AI**，直接进错误界面。
- 进度口径：**已完成数 / 总数**，不能显示"第 n 题"（会倒退）。
- 一轮实际题数 = `N + 补答次数`，UI 要能接受题数比配置的多。

### 5.3 状态机（拼写）

与 5.2 同构，多一个 `revealed`：

| 当前 `task_state` | `revealed=0` 且答对 | 答错 |
|---|---|---|
| `0` | → `1` 完成（从队列移除） | → `-1`，`revealed=1`（**显示正确拼写**），`error_count += 1` |
| `-1` | 用户照着输入正确 → `0`，`revealed=0`（词**留在队列**） | 仍 `-1`，继续显示答案 |
| `1` | — | — |

- `revealed=1` 时输入正确**只回到 0**，不直接完成；下次轮到它时 `revealed=0`，**盲拼**答对才完成。
- 拼写也走环形队列（`served_seq`）：抄写正确后先做别的词，错的词绕一圈再回来。
- 拼写**不参与 FSRS**：不写 `difficulty/stability/due/reps/lapses`，`error_count` 只进 history。
- 拼写 `first_pass = (error_count == 0)`。
- 「我不想拼了」：立即清空该容器的 `__spelling` 表，已完成词的 history 保留，未完成的不写。
- 学习后：拼写本轮 `learning` 表的词；复习后：拼写本轮 `review` 表的词。

### 5.4 一轮结束后的清理

- 任务行**不删除**：结果页、断点续做都依赖它。
- 下一次在同一容器建同类型任务时，**先清空该表**（建任务前的「丢弃」选项执行此操作）。
- 任务表按 `<容器>__<类型>` 命名，所以不同容器的任务互不干扰，可以同时存在多个未完成任务。

### 5.5 中断与恢复

| 中断点 | 现象 | 恢复 |
|---|---|---|
| 建任务后、生成中 | 任务行 `problem_state='pending'` | 主页提示"有未完成任务"，进进度页继续生成 |
| 生成完、学习中 | `task_state != 1` | 从 `next_task()` 接着答 |
| 答完一个词、写回前崩溃 | `task_state=1, applied=0` | 启动时重放写入（4.8） |
| 全部答完、未进结果页 | 全部 `task_state=1` | 直接进结果页 |
| 拼写中途关闭 | `__spelling` 表有残留 | 主页提示"拼写未完成，继续 / 放弃" |

启动时扫描 `tasks.db` 所有表，生成"未完成任务"列表供主页展示。

---

## 6. 算法层（精简版 FSRS）

参数全部来自 `config/fsrs.json`，公式原样保留。

### 6.1 首次学习

```python
def first_learning(first_pass, error_count, now_ts, cfg) -> dict:
    # 完全等于用户给的 calculate_first_learning
    # D = clamp(initial_difficulty + delta + damping*(target_d - initial_difficulty), 1.0, 10.0)
    # delta = -alpha if first_pass else beta * error_count
    # S = initial_stability * (1.8 if first_pass else max(0.3, 1 - 0.3*error_count))
    # 若 not first_pass: S = min(S, cfg.failure_stability_cap_seconds)   ← 唯一补充
    # due = now_ts + S
```

### 6.2 复习

同 `calculate_review`，把 `initial_*` 换成词库现存的 `old_difficulty/old_stability`，失败封顶规则相同。

**兜底**：若词的 D/S 为 `NULL`（脏数据或手工导入），按 `initial_*` 处理，不让 `None` 参与运算。

### 6.3 失败封顶（`failure_stability_cap_seconds`）

- 默认 `86400`（1 天）
- 设 `0` 或 `null` = 关闭，完全等于原公式
- 作用：错过的词最迟 1 天后必须再见面，避免 `S×0.7` 把间隔拖到 21 天

### 6.4 熟练判定

```python
is_mastered = (state == 'review') and (due - now > proficiency_days * 86400)
```

默认 `proficiency_days = 365`。`state='new'` → 学习中；其余 → 复习中。

### 6.5 写回映射

| 字段 | learning | review |
|---|---|---|
| `reps` | `old + error_count + 1` | `old + error_count + 1` |
| `lapses` | `old + error_count` | `old + error_count` |
| `state` | `'review'` | `'review'` |
| `last_review` | `now` | `now` |
| `due` | `now + S_new` | `now + S_new` |
| `last_answer_id` | `answer_id` | `answer_id` |

---

## 7. AI 层

### 7.1 三个接口（对外只有这三个）

```python
AIClient.generate_sentence(word: str, translation: str | None) -> dict
    # → {"sentence": str, "translation": str, "keyword_form": str}
AIClient.score_answer(sentence: str, user_translation: str, keyword: str,
                      reference_translation: str | None) -> dict
    # → {"sentence_score": float, "keyword_score": float}
AIClient.get_balance() -> dict
    # → {"available": bool, "amount": float, "currency": str}
```

- 返回值一律**已校验**：`0 <= score <= 1`，非数字 / 缺键 → 抛 `AIResponseError`
- `keyword_form`：模型实际在句子里用的词形（如 `abandoned`），用于前端高亮；找不到则等于原词
- 评分时**额外传 `reference_translation`**（problem 里存的标准译文），让打分有基准，比只给原句更稳

### 7.2 并发与缓存

```json
"concurrency": { "enabled": true, "mode": "warmup", "workers": 4, "warmup_requests": 1 }
```

| 模式 | 行为 | 适用 |
|---|---|---|
| `serial` | 一次一个 | 最省缓存、最慢 |
| `warmup` | 先发 `warmup_requests` 个并**等它们返回**（写入缓存前缀），其余用 `workers` 路并发 | 默认 |
| `parallel` | 全部并发 | 最快，前缀被重复按 miss 计费 |

`enabled=false` 或 `workers=1` 都退化成串行。缓存命中的前提是 `prompts.json` 的 `system + few_shot` **字节级不变**——改一个字就全失效，这是用户自己要接受的成本。

### 7.3 记忆文件与单写线程

`data/memory/deepseek_memory.json`：

```json
{
  "generate_sentence": { "history": [ {"user": "...", "assistant": "..."} ] },
  "check_answer":      { "history": [ ... ] }
}
```

- 结构固定为两个键，**system 与 few-shot 不放这里**（放 `prompts.json`），避免程序回写覆盖用户手改的提示词
- 两个 section 各自受 `max_history`（默认 2）限制，超出丢最旧的一条
- **所有写入走一个专用写线程**（`queue.Queue` + 单消费者），不靠锁：多线程时若各自「读整文件 → 改 → 写整文件」，后写者会覆盖前者
- 缓存命中靠"前缀稳定"：`messages = [system] + few_shot(user/assistant 对) + 本 section 的历史对 + 本次 user`。历史是追加在后面的，前缀不变，所以仍能命中

### 7.4 重试与限流

| 情况 | 处理 |
|---|---|
| 连接失败 / 超时 | 退避 `0.5s, 1s, 2s` 重试 3 次 |
| HTTP 429 | 读 `Retry-After`，否则退避 `5s, 15s, 45s`；并把本次并发临时降到 1 |
| HTTP 5xx | 同超时 |
| 402 / 余额不足 | **不重试**，立即中止整批，剩余保持 `pending`，向上抛 `InsufficientBalance` |
| JSON 解析失败 / 字段缺失 / 分数越界 | 重试 2 次，仍失败抛 `AIResponseError` |

**关键约定：AI 失败绝不算用户答错**。评分失败时前端提示「评分失败，未计入错误」并给重试按钮，`task_state` 和 `error_count` 都不动。

### 7.5 计费与余额

- 每次响应读 `usage`（`prompt_tokens` / `completion_tokens` / `prompt_cache_hit_tokens`），累加到本轮计数器，供日志与页脚显示
- 余额：`/user/balance` 结果缓存 `balance.refresh_seconds`（默认 300s）
- 检查点：**开始一轮前**（低于警戒线禁止开始）、**每次 AI 请求前**（读缓存值，`<=0` 立即暂停）、**收到 402 时**（刷新一次并暂停）
- 主页底部显示 `余额 ¥x.xx`，低于警戒线标红

---

## 8. 评分与过关

### 8.1 谁判断"过不过"

**AI 只给两个 0~1 的分数，不判断过不过。过不过是本地一行代码算的。**

```
用户提交译文
   │
   ▼
AI: score_answer  ──►  {sentence_score, keyword_score}      ← AI 的活到此为止
   │
   ▼
grading.judge(两个分数, app.pass_rule)  ──►  passed: bool    ← 本地纯函数
   │
   ▼
task_manager.submit(passed)  ──►  task_state +1 / -1
```

为什么不让 AI 直接说"过/不过"，三个理由：

1. **改阈值不用动提示词。** 提示词是缓存前缀，改一个字缓存全失效、全量 miss、全量重算。
2. **判定可复现、可单测。** AI 说"过"不可复现——同一句译文两次调用可能一次给 0.79 一次给 0.81，而 0.8 是硬阈值。
3. **AI 输出少一个字段，就少一种解析失败。**

### 8.2 两个分数各管什么

以句子 `This county is very quiet.`（参考译文：这个县非常安静。关键词：`county`）为例：

| 用户译文 | sentence_score | keyword_score | 说明 |
|---|---|---|---|
| 这个县非常安静。 | 0.95 | 0.95 | 完全对 |
| 这个县很安静。 | 0.85 | 0.90 | 句子基本对，措辞略有出入 |
| 这个国家非常安静。 | 0.85 | **0.20** | 句子结构对，**但关键词译错**（county 是"县"，不是"国家"） |
| 这个县很吵。 | **0.30** | 0.90 | 关键词语义对，**但整句意思反了** |
| 安静 县 | 0.20 | 0.60 | 拼凑关键词，不成句 |

- `keyword_score` 管"**这个词你到底认不认识**"
- `sentence_score` 管"**你有没有真的读懂这句话**"

两个都要，是因为只留一个都会有漏洞：只看词分，用户写"县县县安静"也能骗分；只看句分，用户把句子润色成另一个意思也能过。

### 8.3 两种判定模式

**`and`（默认）**——两个维度都要过线：

```python
passed = (sentence_score >= sentence_min) and (keyword_score >= keyword_min)
```

**`weighted`**——加权总分过线：

```python
passed = (w_sentence * sentence_score + w_keyword * keyword_score) >= total_min
```

同一批分数，两种规则的结果（`and` 用 0.8/0.8；`weighted` 用 0.5/0.5、总分线 0.8）：

| 句分 | 词分 | `and` | `weighted` |
|---|---|---|---|
| 0.95 | 0.95 | ✅ 过 | ✅ 0.950 过 |
| 0.85 | 0.20 | ❌ 词分不够 | ❌ 0.525 不过 |
| 0.30 | 0.90 | ❌ 句分不够 | ❌ 0.600 不过 |
| **0.70** | **1.00** | ❌ 句分差 0.10 | ✅ **0.850 过** |
| **0.75** | **0.90** | ❌ 句分差 0.05 | ✅ **0.825 过** |

**差别只出现在"一个维度很强、另一个略低于线"的情况**：`and` 要求两头都硬，`weighted` 允许互相补偿。

**为什么默认 `and`**：这是背单词，关键词译错就是错，句子再通顺也不该算过——表里第 2 行（0.85 / 0.20）那种在 `and` 下过不了，在 `weighted` 下也过不了，因为 0.2 太低；但第 4、5 行那种"句子差一点"就会在 `weighted` 下被关键词的高分救回来。

如果你的实际体验是**重答次数太多、太虐**，正确的调法是**把 `sentence_min` 降到 0.7**，而不是切到 `weighted`——`weighted` 的副作用是让"关键词译得对、整句理解偏了"的情况混过关，那恰好是这个程序最该抓的东西。

### 8.4 判定结果怎么驱动状态机

`judge()` 只回一个 `bool`，`submit()` 拿它做**唯一一件事**：

| passed | task_state | first_pass | error_count |
|---|---|---|---|
| `True` | +1（上限 1） | 若是本任务行的**首次作答** → `True` | 不变 |
| `False` | -1（下限 -1，不会更负） | 强制 `False`（且永久） | +1 |

这就是"评分"和"状态机"的解耦点：**换规则只改 JSON，状态机一行代码都不用动**。

### 8.5 分数存在哪、界面显示什么

| 位置 | 内容 |
|---|---|
| 任务行 `sentence_score` / `keyword_score` | **每次提交都覆盖为最新一次**（错误界面要显示"你刚才得了多少分"） |
| `history` | 只在**这个词完成时**写一行，带的是完成那次的分数 |
| 学习页 | 两个分数 + 「过关 / 再来一次」，**不显示 AI 评语** |
| 错误界面 | 标准译文 + 单词标准含义 + 你的译文 + 两个分数 + 笔记框 + 「下一题」 |

`history` 里"第一次没过、最后过了"这件事，靠 `first_pass=False` + `error_count` 还原，不靠分数还原。

### 8.6 异常路径

| 情况 | 处理 |
|---|---|
| 分数在 `[0,1]` 之外 | clamp 到 `[0,1]`，继续 |
| 缺键 / 非数字 | 重试 2 次，仍失败抛 `AIResponseError` |
| 重试耗尽 / 网络失败 | **整个提交作废**：`task_state`、`first_pass`、`error_count` 全不动，前端提示「评分失败，未计入错误」并给重试按钮 |

最后这条是硬规则：**AI 出问题，绝不能让用户吃一次错误次数。**

---

## 9. 错误处理矩阵

| 场景 | 检测方式 | 处理 | 用户看到 |
|---|---|---|---|
| 无新词 / 无到期词 | 建任务返回 0 | 不建任务 | 「今天没有新词 / 没有到期单词」 |
| 余额低于警戒线 | 开任务前查缓存余额 | 拒绝开始 | 按钮禁用 + 「余额不足警戒线」 |
| problem 生成失败 | `problem_state='failed'` | 进度页标红，可单条重试 / 全部重试 / 跳过该词 | 红色词条 + 重试按钮 |
| 全部 problem 失败 | 计数 | 不允许开始，回主页 | 「生成失败，请检查网络/余额后重试」 |
| 评分 AI 失败 | 异常 | **不计错**，原地重试 | 「评分失败，未计入错误」 |
| 中途余额耗尽 | 402 / 缓存余额 ≤0 | 中止本批，已完成的已写回，剩余留 `pending` | 「余额不足，任务已暂停」+ 主页显示未完成任务 |
| 进程重启 | 启动扫描 | 重放 `applied=0`；列出未完成任务 | 主页「有未完成的任务，继续？」 |
| 同一容器已有未完成任务 | 建任务前检查 | 询问「继续 / 丢弃重新开始」 | 弹窗 |
| 拼写中途放弃 | 点「我不想拼了」 | 清空 `__spelling`，已完成词 history 保留 | 直接进结果页 |
| 词库表被外部删除 | 查询抛异常 | 当前容器失效，回主页重选 | 「词书不存在」 |
| 写回中途崩溃 | `applied=0` | 幂等重放（4.8） | 无感 |
| 表名非法 / 重名 | 建书校验 | 拒绝 | 输入框报错说明原因 |

---

## 10. 配置

### `config/app.json`

```json
{
  "app_name": "译词",
  "timezone": "Asia/Shanghai",
  "current_container": "cet4",
  "current_container_type": "book",
  "learning_per_round": 20,
  "review_per_round": 50,
  "preview_multiplier": 2,
  "proficiency_days": 365,
  "pass_rule": { "mode": "and", "sentence_min": 0.8, "keyword_min": 0.8,
                 "sentence_weight": 0.5, "keyword_weight": 0.5, "total_min": 0.8 },
  "spelling": { "ask_after_learning": true, "ask_after_review": true },
  "balance": { "warning": 5.0, "refresh_seconds": 300 },
  "server": { "port": 5000, "allow_lan": false },
  "ui": { "show_phonetic": true, "bottom_tabs": ["首页", "仪表盘", "我的"] }
}
```

### `config/fsrs.json`

```json
{
  "initial_difficulty": 3.0,
  "initial_stability": 86400.0,
  "target_d": 3.0,
  "alpha": 0.8,
  "beta": 0.4,
  "damping": 0.02,
  "failure_stability_cap_seconds": 86400,
  "min_difficulty": 1.0,
  "max_difficulty": 10.0
}
```

### `config/ai.json`

```json
{
  "api_key": "sk-...",
  "chat_url": "https://api.deepseek.com/chat/completions",
  "balance_url": "https://api.deepseek.com/user/balance",
  "model": "deepseek-flash",
  "thinking": { "type": "enabled" },
  "reasoning_effort": "low",
  "stream": false,
  "temperature": 0.3,
  "timeout_seconds": 60,
  "max_history": 2,
  "concurrency": { "enabled": true, "mode": "warmup", "workers": 4, "warmup_requests": 1 },
  "retry": { "times": 3, "backoff_seconds": [0.5, 1.0, 2.0] }
}
```

### `config/prompts.json`（稳定前缀，程序只读）

```json
{
  "generate_sentence": {
    "system": "...",
    "few_shot": [ { "user": "china", "assistant": "{\"sentence\":\"...\",\"translation\":\"...\"}" } ],
    "response_format": { "type": "json_object" }
  },
  "check_answer": { "system": "...", "few_shot": [], "response_format": { "type": "json_object" } }
}
```

---

## 11. 路由与页面（草案，界面视觉后续再定）

| 路由 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 主页：今日待学/待复习、余额；底部两侧是「我的」「仪表盘」两个按钮（只在首页出现） |
| `/api/books` | GET/POST | 词书列表 / 切换当前词书 |
| `/study/start` | POST | 校验 → 建任务 → 跳进度页 |
| `/study/prepare` | GET | 进度页（含进度条、失败重试、跳过） |
| `/api/study/progress` | GET | 轮询：`{total, ready, failed, items:[{uid,word,state,error}]}` |
| `/api/study/retry` | POST | 重试单条 / 全部 / 跳过 |
| `/study/session` | GET | 学习页 |
| `/study/correct/<uid>` | GET | 答对页：词义 / 标准译文 / 分数 / 笔记 / 下一题 |
| `/study/error/<uid>` | GET | 答错页：标准译文 / 词义 / 你的答案 / 分数 / 笔记 / 下一题 |
| `/api/study/submit` | POST | 提交翻译 → `{pass, task_state, sentence_score, keyword_score}` |
| `/api/study/skip` | POST | 「我不会」 |
| `/api/study/note` | POST | 保存笔记 |
| `/study/result` | GET | 结果页 |
| `/spelling/start` | POST | 由本轮已完成词生成拼写队列 |
| `/spelling/session` | GET | 拼写页 |
| `/api/spelling/submit` | POST | `{word, correct, revealed, next_hint}` |
| `/api/spelling/abandon` | POST | 「我不想拼了」 |
| `/me` | GET | 二级：在学词书 / 词本 / 统计 / 设置 |
| `/me/wordbank/<name>` | GET | 词书全部单词 + 状态筛选 |
| `/me/tools` | GET | 三级：预习 / 拼写练习 / 生词本 |
| `/me/tools/preview` | GET | 四级：预习词表（2N 个） |
| `/me/wordbooks` | GET/POST | 词本列表 / 新建 |
| `/me/favorites` | GET | 生词本（收藏，跨表 UNION 按 uid 去重） |
| `/me/settings` | GET/POST | 配置读写：API Key、提示词、每轮数量、过关标准、并发、FSRS |
| `/api/prompt/restore` | POST | 恢复出厂提示词（`which` = generate_sentence / check_answer / all） |
| `/unlock` | GET/POST | 非本机设备输入 6 位访问令牌 |
| `/api/server/token` | POST | 重新生成 6 位令牌（会让已解锁设备失效） |
| `/api/reset` | POST | 初始化：清空任务、历史、单词进度、笔记、收藏、预习记录、AI 记忆；词书保留 |
| `/dashboard` | GET | 统计 + 词书切换菜单 |
| `/api/balance` | GET | 余额（带缓存） |

---

## 12. 模块接口签名

```python
# services/scheduler.py  —— 纯函数，无 IO，可单测
def first_learning(first_pass: bool, error_count: int, now_ts: float, cfg: dict) -> dict
def review_pass(first_pass: bool, error_count: int, now_ts: float,
                old_difficulty: float, old_stability: float, cfg: dict) -> dict
# → {"difficulty": float, "stability": float, "next_review": float}

# services/grading.py  —— 纯函数
def judge(sentence_score: float, keyword_score: float, rule: dict) -> bool

# services/clock.py
def now_ts() -> float
def end_of_today_ts() -> int
def is_mastered(due: int, now: float, proficiency_days: int) -> bool

# services/task_manager.py  —— 只做 DB，不碰 AI、不碰 Flask
class TaskManager:
    def __init__(self, library, wordbooks, tasks, history, cfg)
    # 建/清任务
    def create_task(self, container: str, ctype: str, kind: str, limit: int) -> list[dict]
    def discard_task(self, container: str, ctype: str, kind: str) -> None
    def list_unfinished(self) -> list[dict]          # [{container, ctype, kind, done, total}]
    # problem
    def pending_problems(self, container, ctype, kind) -> list[dict]
    def set_problem(self, container, ctype, kind, uid, problem: dict) -> None
    def fail_problem(self, container, ctype, kind, uid, error: str) -> None
    def skip_problem(self, container, ctype, kind, uid) -> None
    # 答题
    def next_task(self, container, ctype, kind) -> dict | None
    def submit(self, container, ctype, kind, uid, answer: str,
               sentence_score: float | None, keyword_score: float | None,
               passed: bool, answer_id: str) -> dict   # {task_state, finished, round_finished}
    def apply_to_wordbank(self, container, ctype, kind, uid, answer_id) -> None
    # 拼写
    def create_spelling(self, container, ctype, source_kind: str) -> int
    def next_spelling(self, container, ctype) -> dict | None
    def submit_spelling(self, container, ctype, uid, typed: str) -> dict
    def abandon_spelling(self, container, ctype) -> None
    # 结果
    def round_summary(self, container, ctype, kind) -> list[dict]

# services/ai_client.py
class AIClient:
    def generate_sentence(self, word, translation=None) -> dict
    def score_answer(self, sentence, user_translation, keyword, reference=None) -> dict
    def get_balance(self) -> dict

# services/ai_pool.py
class AIPool:
    def generate_many(self, items: list[dict], on_done=None, on_error=None) -> list[dict]

# services/balance.py
class Balance:
    def current(self, force=False) -> dict
    def can_start(self) -> bool
```

---

## 13. 测试计划

| 文件 | 覆盖 |
|---|---|
| `test_scheduler.py` | 首次学习/复习的 D/S/due；边界（error_count=0、clamp 到 1/10、封顶开关）；`first_pass=True` 时 error_count 被强制置 0 |
| `test_task_manager.py` | 状态机 -1/0/1 全路径；`first_pass` 永久 False；`applied` 幂等重放；拼写状态机含 `revealed`；`next_task` 不重复出题 |
| `test_clock.py` | `end_of_today_ts` 跨日、时区 |
| 手工 | 中文词书名建 / 改名 / 删除；余额不足中断；进程 kill 后恢复 |

---

## 14. 未决 / 待实测（不阻塞设计，开工时验证）

1. `deepseek-flash` + `thinking` + `reasoning_effort` 三个参数是否被 API 接受 —— 用你给的 key 实调一次，被拒就降级成只发 `model + messages`。
2. 中文表名在 `dataset.create_column`（补列）路径上的行为 —— 建表/改名/删除已实测通过，补列未测。
3. 词形变化高亮：句子可能用 `abandoned` 而 `word` 是 `abandon`，服务端做「小写包含匹配」，匹配不到就整句不高亮，并在 prompt 里要求优先用原形。
4. 界面视觉（配色、字体、卡片、动画）—— 按你说的，逻辑定完再逐页设计。

### 实测结果（2026-11-22 补记）

- **`deepseek-flash` + `thinking` + `reasoning_effort` + `response_format` 全部被接受**，
  造例句 0.9s、评分 1.1s。余额接口返回 ¥11.45。所以 §7 的降级逻辑保留但不触发。
- 中文表名、带空格表名、`ALTER TABLE RENAME`、`DROP TABLE`、`create_column` 补列均正常。
- 端到端跑通：建任务 → 并发生成 3 个 problem → 逐词提交 → 状态机推进 → 结果页 →
  拼写（错 → 抄写 → 盲拼）→ 历史落库。

---

## 15. 实现与文档的差异（以代码为准）

1. `answer_id` 改为**确定性派生**（见 4.8 补注），`history` 表因此多一列 `answer_id` 并建唯一索引。
2. Windows 没有 IANA 时区库，`clock.tzinfo()` 在 `ZoneInfo` 失败时退化为固定偏移
   （`Asia/Shanghai` → `+08:00`，上海 1991 年后无夏令时，等价）。
3. 测试用标准库 `unittest`（本机 PyPI 不可达装不了 pytest），命令：
   `python -m unittest discover -s tests -t .`
4. 本机依赖安装方式：`python -m venv .venv --system-site-packages`（继承全局 Flask/requests），
   再把 `dataset` / `sqlalchemy` / `alembic` / `mako` / `typing_extensions` 从
   `refuse_to_memorize_words/.venv` 复制进来。`requirements.txt` 是给能联网的机器用的。
5. 预习的"看到第几个"存在 `config/app.json` 的 `preview.<容器名>.index`，
   词级"看过"时间戳存在 `word_bank.previewed_at`。
6. 词书详情页路由是 `/me/wordbank/<name>`，与 `/me/tools` 平级，两者都是三级页。
7. **数据库连接用 `AUTOCOMMIT`**。`dataset` 的 `db.query()` 不会自动提交，
   留着的未提交事务会长期占着 SQLite 写锁，表现为下一次写操作报
   `database is locked`。单用户场景不需要显式事务，所以每条语句自己提交。
8. **`mark` 过滤器返回 `Markup`**。内部已 `html.escape` 一次，若再让 Jinja 自动转义，
   `<mark>` 会变成字面量、`'` 会变成 `&#x27;`。
9. **拼写残留启动即清**：`create_app` 里调 `TaskManager.cleanup_spelling()`，
   把所有 `*__spelling` 表清空。拼写不做断点续做，已完成词的 history 不受影响。
   同时 `DBManager._connect` 在开连接前先 `wal_checkpoint(TRUNCATE)`，
   收掉上次异常退出留下的 WAL。
10. **导航只在首页**：底部两侧放「我的」「仪表盘」两个悬浮按钮，二级页与学习流程里都不显示，
    靠左上角返回箭头回首页。没有任何常驻底栏。
11. **桌面优先的版式**：外壳最大宽度 1040px，首页两栏（签到卡 + 学习/复习大按钮），
    仪表盘与我的页面两栏卡片，单词表/预习/结果列表两列，学习类页面收窄到 820px 保证阅读。
12. **空白提交直接算错**：`/api/study/submit` 收到空串（含全空白）时不调 AI，
    直接按 `passed=False` 走状态机并进错误页——省 token，也省一次等待。
13. **初始化（`/api/reset`）**：清任务 / 历史 / 每个词的 D·S·due·reps·lapses /
    笔记 / 收藏 / 预习记录 / AI 记忆；**词书与词条保留**。可选同时清空 API Key，
    方便把项目直接交给别人。
14. **提示词可改**：设置页直接编辑两段 `system`，留空不保存（防误删）；
    "恢复默认"读 `config/prompts.default.json`，缺失时回退到包内的
    `app/defaults/prompts.json`。API Key 与两段提示词的当前值都直接显示在输入框里。
15. **端口与访问范围**：`app.json` 的 `server.port` / `server.allow_lan` 由设置页维护，
    `run.py` 启动时读取（命令行 `--port` / `--allow-lan` 可临时覆盖，`--allow-lan` 会
    同时放行守卫）。默认绑 `127.0.0.1`，并且有 `before_request` 守卫：`allow_lan` 关闭时，
    任何非环回地址的请求直接 403——即使误绑了 `0.0.0.0` 也挡得住。
16. **6 位访问令牌**：`allow_lan` 打开后，非本机设备首次访问会被重定向到 `/unlock`，
    输入电脑上显示的 6 位数字（`server.token`）才放行；成功后写进签名会话 cookie。
    令牌被改过则旧会话失效；同 IP 60 秒内最多试 5 次。会话密钥是 `server.secret`
    里的随机串，不从源码推导，所以拿到源码也伪造不出"已解锁"。

### 15.9 例句历史与提示词（2026-11-23）

**为什么要记历史**：同一个词反复出现时，AI 容易给出几乎一样的句子，不利于记忆。现在每个词会记住最近若干条生成过的句子，下次生成时作为"要避开"的清单传给模型。

数据落点：

| 位置 | 列 | 内容 |
|---|---|---|
| 词书/词本表 | `sentences` | JSON 数组，该词历史例句，去重、只留最近 N 条 |
| 任务表（learning/review） | `recent_sentences` | 生成这道题时实际传入的 avoid 列表（快照，便于回看） |

- N 来自 `config/app.json` 的 `sentence_history`（默认 5，0 = 关闭），设置页可改
- 老库在启动时自动补列（`ensure_container_table` / `ensure_task_table` 会逐表 `create_column`）
- 生成成功后在 `TaskManager.set_problem` 里追加；`_parse_sentences` 负责容错解析

**提示词改成 JSON 进、JSON 出**：

```json
// 输入
{"word": "abandon", "translation": "vt. 放弃", "avoid_sentences": ["...", "..."]}
// 输出（system 里明确定义这三个键）
{"sentence": "...", "translation": "...", "keyword_form": "..."}
```

`avoid_sentences` 会随 problem 一起回传，`set_problem` 取出来写进任务行并从 problem 里剔除，不污染题目本体。

### 15.10 复习最短间隔（2026-11-23）

**问题**：答错会让稳定性 S 掉到小时级（86400 × 0.7 = 16.8h，连续答错更低），而选词条件是 `due <= 今天 23:59:59`，于是同一个词同一天被抽到好几次。

**做法**：只给到期时间加下限，不改 S。

```python
next_review = max(now + S, now + min_interval_seconds)
```

- 配置在 `config/fsrs.json` → `min_interval_seconds`，默认 `86400`（24 小时），`0` = 关闭
- 设置页「两次复习最短间隔（小时）」可改，存盘时换算成秒
- 学习轮和复习轮都生效；**同一轮内**答错词的环形重现不受影响（那是刻意设计的即时强化）
- 不改 S 的原因：如果连 S 一起抬，`S × 1.8` 叠加下限会让间隔膨胀过快；只顶 due 的话，S 自然长过 24h 后下限就自动失效
- 旧数据不迁移：已在今天到期且 due 早于 `now + 24h` 的词，今天仍会被抽到，明天起自然收敛

### 15.11 任务行 id 复用导致作答被丢弃（2026-11-23，重要修复）

**现象**：明明答对完成了，词表的 `last_review/due/S` 却不变，于是旧的到期时间一直生效——看起来像"最短间隔没起作用"、"同一个词反复出现"。

**根因**：`create_task` 每次都 `DELETE FROM <任务表>` 再插入，SQLite 对非 AUTOINCREMENT 的行会**从 1 重新分配 rowid**。而 `answer_id = uuid5(ctype|container|kind|row_id)`，两轮之间同一个位置的任务行会得到**相同**的 answer_id。`_apply` 里有"已结算就跳过"的保护：

```python
if word.get("last_answer_id") != answer_id:   # 上一轮存下的 same id → 跳过
```

于是新一轮的作答被当成重复请求丢掉，`applied` 却仍然是 1。

**修复**：任务表加 `batch_id` 列（`create_task` / `create_spelling` 每次生成一个 uuid4 hex），answer_id 改为 `uuid5(ctype|container|kind|batch_id|row_id)`。同一轮内仍然幂等（重放安全），跨轮次不会撞车。

回归测试 `tests/test_answer_id.py`：
- 两轮之间 rowid 相同但 answer_id 不同
- 第二轮作答能正常写回
- 两轮各写一条 history
- 同一批次内重复 `_apply` 仍然只写一次
