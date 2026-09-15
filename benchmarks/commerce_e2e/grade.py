"""Numeric and evidence prerequisites; narrative/decision soundness needs human review."""
import json,re
import sqlglot
from sqlglot import exp
from oracle import compare

def grade(case,result,expected):
    answer=result.get('answer_full','') or ''
    blocks=re.findall(r'<benchmark_result>\s*(.*?)\s*</benchmark_result>',answer,re.S)
    actual=None;errors=[]
    if len(blocks)!=1:errors.append(['result_block','exactly one result block required',len(blocks)])
    else:
        try:actual=json.loads(blocks[0]);errors+=compare(expected,actual)
        except (ValueError,TypeError) as e:errors.append(['result_block','invalid JSON',str(e)])
    ledger=result.get('executions',{});refs=set(re.findall(r'\bQ\d+\b',answer));tables=set();badrefs=[]
    for ref in refs:
        entry=ledger.get(ref)
        if not entry or entry.get('payload',{}).get('status') not in ('ok','empty'):
            badrefs.append(ref);continue
        sql=entry.get('sql') or entry.get('args',{}).get('sql')
        if not sql:continue
        try:tables.update(t.name for t in sqlglot.parse_one(sql,read='sqlite').find_all(exp.Table))
        except Exception:badrefs.append(ref+':sql_parse')
    missing=sorted(set(case['tables'])-tables)
    if not refs:badrefs.append('no_actual_query_reference')
    numeric='PASS' if not errors else 'FAIL'
    evidence='PASS' if not badrefs and not missing else 'FAIL'
    return {'case_id':case['id'],'status':'REVIEW_REQUIRED' if result.get('status')=='ok' and numeric==evidence=='PASS' else 'FAIL',
        'agent_status':result.get('status','no_result'),'numeric_status':numeric,'numeric_errors':errors,
        'evidence_prerequisites':evidence,'invalid_references':sorted(badrefs),'missing_cited_tables':missing,
        'cited_tables':sorted(tables),'review_items':case['review'],
        'scope':'自动检查核对全部目标值、引用存在性及成功取数覆盖；不证明SQL语义正确、结论因果成立或每个数字绑定正确证据。人工复核通过后才能记整条PASS。'}
