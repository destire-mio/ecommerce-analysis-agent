"""Harness tests, never a substitute for Agent E2E execution."""
from pathlib import Path
import json,copy,sys
from cases import CASES
from grade import grade
import platform_mcp
from oracle import compare

root=Path(sys.argv[1]).resolve();gold=json.loads((root/'private/expected.json').read_text())
checks=[]
def check(name,condition):
    if not condition:raise AssertionError(name)
    checks.append(name)
for case in CASES:
    # Synthetic ledger only tests grading mechanics, not evidence semantics.
    result={'status':'ok','answer_full':'审核用模拟输出 (Q1)\n###EVIDENCE###\n<benchmark_result>'+json.dumps(gold[case['id']],ensure_ascii=False)+'</benchmark_result>',
        'executions':{'Q1':{'sql':'SELECT 1 FROM '+','.join(case['tables']),'payload':{'status':'ok'}}}}
    check(case['id']+' exact gold requires review, not automatic pass',grade(case,result,gold[case['id']])['status']=='REVIEW_REQUIRED')
    changed=copy.deepcopy(gold[case['id']]);obj=changed
    while isinstance(next(iter(obj.values())),dict):obj=next(iter(obj.values()))
    key=next(k for k,v in obj.items() if type(v) is int);obj[key]+=1
    corrupt=copy.deepcopy(result);corrupt['answer_full']=corrupt['answer_full'].replace(json.dumps(gold[case['id']],ensure_ascii=False),json.dumps(changed,ensure_ascii=False))
    check(case['id']+' one-unit mistake rejected',grade(case,corrupt,gold[case['id']])['numeric_status']=='FAIL')
    bad=copy.deepcopy(result);bad['answer_full']=bad['answer_full'].replace('(Q1)','(Q999)')
    check(case['id']+' invented evidence rejected',grade(case,bad,gold[case['id']])['evidence_prerequisites']=='FAIL')
    bad=copy.deepcopy(result);bad['executions']['Q1']['payload']['status']='error'
    check(case['id']+' failed query not evidence',grade(case,bad,gold[case['id']])['evidence_prerequisites']=='FAIL')
check('missing report rejected',grade(CASES[0],{'status':'ok'},gold['C1'])['status']=='FAIL')
check('bool is not money',bool(compare(1,True)))
check('NaN is not a rate',bool(compare(0.5,float('nan'))))
platform_mcp.DB=root/'public/platform.sqlite'
check('real platform aggregate',platform_mcp.query('SELECT COUNT(*) n FROM inventory_snapshots')['rows']==[[80000]])
check('empty result is successful empty',platform_mcp.query('SELECT * FROM ad_daily WHERE shop_id=-1')['n_rows']==0)
check('missing source is an error','error' in json.loads(platform_mcp.query_platform('SELECT * FROM missing_table')))
check('large result explicitly rejected',platform_mcp.query('SELECT * FROM inventory_snapshots')['error']=='result_too_large')
check('mutation rejected','error' in json.loads(platform_mcp.query_platform('DELETE FROM inventory_snapshots')))
check('multiple statements rejected','error' in json.loads(platform_mcp.query_platform('SELECT 1; SELECT 2')))
report={'status':'PASS','checks':checks,'count':len(checks),'scope':'fixture SQL and grader checks; not Agent E2E PASS'}
(root/'private/harness_selfcheck.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
