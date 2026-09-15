"""Summarize observed runs, preserving input-version boundaries."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parent
OLD=ROOT/'artifacts/baseline-20260914'
NEW=ROOT/'artifacts/baseline-v2-c5-20260914'
DATA=ROOT/'artifacts/scale-1m-v2-seed9142026'

def extract(cid,base):
 work=base/cid;assessment=json.loads((work/'assessment.json').read_text())
 trace=list(work.glob('runs/*/trace.jsonl'))[0];events=[json.loads(x) for x in trace.read_text().splitlines()]
 usage=[e.get('usage') or {} for e in events if e.get('action')=='llm']
 return {'id':cid,'base':str(base.relative_to(ROOT)),'status':assessment['status'],'agent_status':assessment['agent_status'],'seconds':assessment['elapsed_seconds'],
  'rounds':len(usage),'calls':sum(e.get('action')=='tool_call' for e in events),'prompt_tokens':sum(e.get('p',0) for e in usage),'completion_tokens':sum(e.get('c',0) for e in usage),
  'trace':str(trace.relative_to(ROOT)),'last_event':events[-1],'runtime_sha256':assessment['runtime_sha256']}

if not (NEW/'C5/assessment.json').exists():raise SystemExit('C5 re-run is still running; no final report generated.')
rows=[extract(c,OLD if c!='C5' else NEW) for c in ['C1','C2','C3','C4','C5']]
sha=hashlib.file_digest((ROOT.parents[1]/'agent_loop.py').open('rb'),'sha256').hexdigest()
assert all(x['runtime_sha256']==sha for x in rows)
manifest=json.loads((DATA/'manifest.json').read_text())
for db,h in manifest['database_sha256'].items():assert hashlib.file_digest((DATA/'public'/db).open('rb'),'sha256').hexdigest()==h
if any(x['status']=='REVIEW_REQUIRED' for x in rows):raise SystemExit('Human review required before publishing the final baseline report.')
passed=sum(x['status']=='PASS' for x in rows)
notes={
'C1':'第6轮把traffic_daily发给订单库execute_sql；SQL门禁预检抛出未捕获的缺表异常。',
'C2':'15轮预算耗尽。发生过平台表选错数据源、WITH被门禁拒绝与退款查询超时；最后停在商品毛利计算，未交付活动贡献报告。',
'C3':'第7轮退款关联查询发出后没有工具返回；执行器在420秒截止终止进程。轨迹能定位停留点，尚不足以断言唯一性能根因。',
'C4':'第12轮复购查询的cid字段歧义触发SQL门禁预检异常，进程退出。',
'C5':'修正版运行到15轮预算耗尽。进行了订单ID跨源取数，发生过把finance_entries发给订单库、把fx_daily发给平台库的错误；没有交付完整对账桥报告。'}
lines=['# 现有Agent基线结果','',f'本次5条任务的端到端验收通过数：**{passed}/5**。标准答案的SQL复核通过与Agent完成任务是两项结果。没有最终报告时，不把它描述成所有数字都算错。','',
'## 运行条件','',
'2026-09-14，本地主机，现有`.venv`与`agent_loop.py`正式CLI，产品配置模型`deepseek-flash`、temperature=0、15轮预算。原始运行同时执行2条任务，每条420秒截止；对账修正重跑只执行1条。时间包括模型请求、MCP连接与数据库执行，不能当SQL性能或生产SLA。', '',
'平台适配器是本Benchmark提供的通用原始表查询MCP服务，取代小型平台夹具；产品运行时代码没有改动。以下是“原运行时 + Benchmark数据适配器”的评测，不代表原有平台接口不经适配就支持这些数据。', '',
'## 用例结果','',
'| 用例 | 正式入口状态 | LLM轮次 | 工具调用 | 用时/秒 | 本次观测 |','|---|---|---:|---:|---:|---|']
for x in rows:lines.append(f"| {x['id']} | {x['agent_status']} | {x['rounds']} | {x['calls']} | {x['seconds']} | {notes[x['id']]} |")
lines+=['','## 输入版本与对账用例修正','',
'首版数据的结算间隔与订单号取模相关。C5在首轮执行中从样本推断出该规律，使用`order_id%5+1`代替全量跨源匹配。即使个别合计吻合，这种未经业务规则授权的推断不能作为对账证据。原始运行仍保留供审查，不纳入修正版C5结论。','',
'当前v2把结算间隔改成种子驱动的分散取值。71.5万笔已到账商品记录不符合旧公式；C5必须依据实际流水判断到账日期。v2的5条标准答案重新通过SQL复核。','',
'C1至C4沿用首轮轨迹；并非声称又跑了一次。已核对订单数据库字节哈希相同，流量、广告、库存、采购单、供应商条款逐行哈希相同，C1至C4标准答案相同；这4条实际依赖的输入没有变化。C5在v2上单独重跑。输入一致性证据见[核对结果](artifacts/scale-1m-v2-seed9142026/private/v1_v2_input_equivalence.json)。','',
'## 这份结果可以说明什么','',
'现有Agent的多源取数路由、复杂查询恢复、跨源中间结果处理与最终交付控制尚不足以完成这组任务。C2、C5执行过多项有效查询和计算；预算耗尽不等于没有分析能力，但经营用户没有收到要求的最终交付。','',
'自动验收器当前核对目标数值、成功引用与来源覆盖，因果判断及逐结论证据对应仍需要人工审查。本次没有用人工写出的答案补齐Agent报告，也没有修改产品代码让基线通过。','',
'## 可复查证据','']
for x in rows:
 b=x['base']+'/'+x['id'];lines.append(f"- {x['id']}：[执行轨迹]({x['trace']}) · [入口返回]({b}/agent_result.json) · [错误日志]({b}/stderr.log) · [验收详情]({b}/assessment.json)")
lines+=['','- [v2标准答案SQL复核](artifacts/scale-1m-v2-seed9142026/private/oracle_verification.json)','- [数据复杂度与金额恒等式核查](artifacts/scale-1m-v2-seed9142026/private/data_audit.json)','- [29项评测器与适配器检查](artifacts/scale-1m-v2-seed9142026/private/harness_selfcheck.json)','',
'运行结束后，v2两个公开数据库的SHA-256与生成清单一致；各用例运行时代码SHA-256与产品源文件一致。源码未提交，数据和运行产物在本地保存。','']
(ROOT/'基线报告.md').write_text('\n'.join(lines),encoding='utf8')
(ROOT/'artifacts/effective_baseline.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
print(json.dumps({'case_count':len(rows),'pass_count':passed,'rows':[{k:x[k] for k in ['id','agent_status','rounds','calls','seconds']} for x in rows]},ensure_ascii=False,indent=2))
