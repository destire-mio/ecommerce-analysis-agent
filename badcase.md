# Bad Cases（第 3 轮评测：15/20）

> 考卷：`data/exam.json`（Spider dev 抽样 20 题，easy 8 / medium 8 / hard 4）
> 判分：行集合比对（忽略行序），agent SQL 与 gold SQL 各自执行后逐行对值
> 全量结果：`results/exam.json`（easy 8/8, medium 5/8, hard 2/4）

---

## 归因分类（目前两类）

1. **口径分歧型**：agent 的 SQL 本身合理、执行正确，只是与 gold 的标注口径不同（去重/不去重、分组粒度）。
2. **语义错误型**：SQL 逻辑真错（如集合运算用错），稳定复现，重试无济于事。

---

## 逐题记录

### 题 0 [medium / wta_1] — 口径分歧型

- **问题**：What are the first names of all players, and their total ranking points?
- **gold**：`GROUP BY first_name`——同名球员积分合并，1580 行（1580 个不同名字）
- **agent**：`GROUP BY player_id, first_name`——每个球员一行，真实 2775 行（被 Runtime 的 LIMIT 1000 砍到 1000）
- **重跑验证**：temperature=0，重跑后 SQL 相同、错误相同 → 可复现
- **备注**：题目有歧义（"all players" 按谁分组两可）；agent 口径从用户视角也合理，但考卷以 gold 为准
- **动作**：不计为 agent 能力问题（用户决定）；判分器需修正——比对前剥掉 Runtime 自动加的保护 LIMIT

### 题 2 [medium / concert_singer] — 口径分歧型

- **问题**：What are the names of the singers who performed in a concert in 2014?
- **gold**：逐行配对，同一歌手演 2 场出现 2 次 → 6 行
- **agent**：多加了 `DISTINCT`（去重）→ 5 行
- **备注**：用户视角 agent 更合理（要名单），gold 忠于逐行配对；与题 0 同类

### 题 4 [hard / tvshow] — 语义错误型（稳定复现）

- **问题**：列出既播过 Ben Jones 导演的卡通、又播过 Michael Chang 导演的卡通的频道及国家
- **gold**：`INTERSECT`（交集）→ 1 行（MTV Dance）
- **agent**：`WHERE Directed_by IN ('Ben Jones', 'Michael Chang')`（"或"语义）→ 6 行
- **重跑验证**：重跑 3 次，SQL 一字不差，全部 FAIL → 稳定失败，重试无用
- **根因**："既…又…"（AND）被理解为"任一"（OR）；`IN` 是"或"不是"且"
- **动作**：候选修法——prompt 里加"既…又…"→INTERSECT 的示例；修好后回归验证

### 题 7 [medium / concert_singer] — 待归因

- 症状：行数 5 vs 6，疑似与题 2 同类（DISTINCT），待逐题确认

### 题 13 [hard / student_transcripts_tracking] — 待归因

- 症状：行数相同但内容不匹配，待逐题确认

---

## 判分器待修正（影响分数可信度）

- [ ] 比对前剥掉 Runtime 自动加的保护 `LIMIT`（否则 agent 语义对但行多会被误判）
- [ ] 考虑 gold 含重复行 vs agent 去重的评分宽容度（口径分歧是否单独记一类，不占 agent 分）

## 待办

- [ ] 题 7、题 13 逐题归因
- [ ] 5 题归因齐后，统计两类占比，决定修 prompt 还是修判分
- [ ] 同一失败案例（题 4）修复后重跑回归验证
- [ ] token 记账已接入 runtime（下次运行生效）

---

## 电商场景 · Golden Case 自身错误案（2026-09-13）

- **题**: 服饰里卖得最好的商品是哪个？卖了多少？
- **现象**: agent 答 P0004=20,629 元; golden_cases 说 P0016=1,541 元 → 冲突
- **查证**: SQL 聚合证实 P0004=20,629 为商品销售额第一; golden 的 1541 实为
  "单笔最大明细行金额"(生成器把'单品最大行'误当成'商品总销售额')
- **裁决**: **agent 对, 标准答案错**; 题目语义"卖得最好"=按商品聚合
- **修正**: golden_cases 题7 v2(附修正说明)
- **教训**: ① 标准答案也要被挑战——agent 与 gold 冲突时先查证再裁决;
  ② 生成 Golden Case 时, "问题语义→统计口径"必须显式(聚合粒度写进 expected)

---

## 电商场景 · 同秒并发撞会话 + 批准材料漂移（2026-09-14）

### 案 1：同秒起跑的两个新会话撞 session id

- **现象**: G9/G10 两题并行起跑（新会话），session id `s-YYYYMMDD-HHMMSS` 只精确到秒，
  同秒同名 → G10 实际"恢复"了 G9 的会话，把 G9 的问答端进自己的上下文，首轮 G10 结果作废
- **修复**: `agent_loop.py` 新增 `claim_dir()`——时间戳+4位随机尾 + **os.mkdir 独占认领**
  （同名当场 FileExistsError → 换尾重试）。会话与 run(trace) 目录同病根一并修。
  指定 `--session` 的恢复路径行为不变，老会话不受影响
- **验证**: 同秒连开 50 目录全唯一 ✓；真实运行新会话 ✓；追问恢复跨轮 Qn 引用 ✓
- **教训**: 并发身份分配不能靠"概率小"，要让文件系统当裁判（独占创建），撞了必改名

### 案 2：approved_sql.json（批准 SQL 快照）与当前库三处不同步

- **现象**: S3 验证运行中 agent 报"近7天订单 8,000 单、单均 100 元"，与 approved_sql.json
  写的 4,000 单/单均 200 冲突
- **查证**: 直查库——订单(去重)=8,000、单均=100.0，**agent 对，approved_sql.json 是旧版**；
  另发现其品类名(服饰/家居)也与当前库(家电/其他行业)漂移（数字量级一致）
- **影响**: 该文件已不能当"批准口径"的可靠锚点引用；golden_cases_v2 不受影响
  （判分用 numbers+语义词，未引用这些过期值）
- **教训**: ① 批准材料本身会漂移——"agent 念的口径以谁为准"必须有比快照文件更强的锚；
  ② 修库/换数据时，凡自称"批准/标准"的文件要和库一起重生成或加版本戳

### 附带观察：口径答案不含"指标负责人"

- PRD L01 要求定义/版本/**负责人**/生效时间/单位；注册表(metrics.yaml)有 owner 字段，
  但手册卡没写、agent 读不到 → 答案从不提负责人。材料一行字的缺口，记账待补
