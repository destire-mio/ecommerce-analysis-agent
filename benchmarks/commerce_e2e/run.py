"""Run the product's unchanged formal CLI, isolated per case. No gold in model context."""
from pathlib import Path
import argparse,json,os,sys,shutil,hashlib,time,subprocess,signal
from concurrent.futures import ThreadPoolExecutor,as_completed
from cases import CASES,prompt
from grade import grade

HERE=Path(__file__).resolve().parent
PRODUCT=HERE.parents[1]
def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def sha(p):return hashlib.file_digest(p.open('rb'),'sha256').hexdigest()

def execute(case,data,out,python,timeout,expected):
    work=out/case['id'];work.mkdir()
    for path in ['data/ecommerce/skills','data/platform','tools']:(work/path).mkdir(parents=True,exist_ok=True)
    shutil.copy2(PRODUCT/'agent_loop.py',work/'agent_loop.py')
    shutil.copy2(HERE/'contract.md',work/'data/ecommerce/skills/commerce_contract.md')
    shutil.copy2(HERE/'platform_mcp.py',work/'tools/platform_mcp.py')
    (work/'data/ecommerce/ecommerce.sqlite').symlink_to(data/'public/warehouse.sqlite')
    (work/'data/platform/platform_data.sqlite').symlink_to(data/'public/platform.sqlite')
    question=prompt(case);(work/'question.txt').write_text(question)
    argv=[str(python),str(work/'agent_loop.py'),'--question',question,'--db','ecommerce','--bench','ecommerce','--session',case['id']]
    started=time.monotonic();state={};timedout=False
    with (work/'stdout.log').open('w') as stdout,(work/'stderr.log').open('w') as stderr:
        proc=subprocess.Popen(argv,cwd=work,stdout=stdout,stderr=stderr,start_new_session=True)
        try:proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:timedout=True
        finally:
            # CLI MCP bridges have their own threads/process; clean entire process group.
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            if timedout:
                try:proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL);proc.wait()
    for line in reversed((work/'stdout.log').read_text().splitlines()):
        try:
            obj=json.loads(line)
            if isinstance(obj,dict) and 'status' in obj:state=obj;break
        except ValueError:pass
    if not state:state={'status':'timeout' if timedout else 'process_error'}
    dump(work/'agent_result.json',state)
    (work/'answer.md').write_text(state.get('answer_full','') or '本次未得到最终报告。状态：'+state['status'])
    assessment=grade(case,state,expected)
    assessment.update({'elapsed_seconds':round(time.monotonic()-started,2),'exit_code':proc.returncode,'timeout':timedout,'runtime_sha256':sha(work/'agent_loop.py')})
    # Trace analysis remains useful when the product exhausts its iteration budget.
    events=[]
    for trace in (work/'runs').glob('*/trace.jsonl'):
        for line in trace.read_text().splitlines():
            try:events.append(json.loads(line))
            except ValueError:pass
    assessment['trace_paths']=[str(p.relative_to(out)) for p in (work/'runs').glob('*/trace.jsonl')]
    assessment['trace_events']=len(events)
    dump(work/'assessment.json',assessment)
    print(json.dumps({k:assessment[k] for k in ['case_id','agent_status','numeric_status','status','elapsed_seconds']},ensure_ascii=False),flush=True)
    return assessment

def main():
    a=argparse.ArgumentParser();a.add_argument('dataset',type=Path);a.add_argument('--out',type=Path,required=True);a.add_argument('--case',choices=[c['id'] for c in CASES],action='append');a.add_argument('--workers',type=int,default=1);a.add_argument('--timeout',type=int,default=480);a.add_argument('--python',type=Path,default=PRODUCT/'.venv/bin/python');args=a.parse_args()
    if not os.environ.get('DEEPSEEK_API_KEY'):raise SystemExit('BLOCKED: formal Agent requires DEEPSEEK_API_KEY. No fixture fallback was run.')
    data=args.dataset.resolve();out=args.out.resolve()
    verified=json.loads((data/'private/oracle_verification.json').read_text())
    if verified['status']!='PASS':raise SystemExit('Reference answers are not verified.')
    if out.exists():raise SystemExit('Use a NEW --out directory; previous runs are immutable.')
    out.mkdir(parents=True)
    expected=json.loads((data/'private/expected.json').read_text());manifest=json.loads((data/'manifest.json').read_text())
    if {p.name:sha(p) for p in (data/'public').glob('*.sqlite')}!=manifest['database_sha256']:raise SystemExit('Dataset hash mismatch')
    metadata={'dataset':str(data),'manifest_sha256':sha(data/'manifest.json'),'product_runtime_sha256':sha(PRODUCT/'agent_loop.py'),'adapter_sha256':sha(HERE/'platform_mcp.py'),'contract_sha256':sha(HERE/'contract.md'),'cases_sha256':sha(HERE/'cases.py'),'python':str(args.python),'workers':args.workers,'timeout_seconds':args.timeout,'boundary':'isolated working directory/process, shared host; NOT OS security sandbox or production performance certification; real Agent, benchmark raw-data MCP adapter'}
    dump(out/'run_manifest.json',metadata)
    selected=[c for c in CASES if not args.case or c['id'] in args.case];results=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(execute,c,data,out,args.python,args.timeout,expected[c['id']]) for c in selected]
        for future in as_completed(futures):
            results.append(future.result());dump(out/'summary.json',sorted(results,key=lambda x:x['case_id']))
    print('Results: '+str(out/'summary.json'))
if __name__=='__main__':main()
