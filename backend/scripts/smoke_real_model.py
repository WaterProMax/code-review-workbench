import asyncio, getpass, hashlib, json, logging, os, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.providers.deepseek import DeepSeekClient
from app.providers.base import LLMRequest, ChatMessage

async def main():
    key = getpass.getpass('DeepSeek API key (hidden): ')
    # Existing HTTP(S) proxy is sufficient; avoid unused SOCKS fallback requiring socksio.
    for proxy_name in ['ALL_PROXY', 'all_proxy']:
        os.environ.pop(proxy_name, None)
    client = DeepSeekClient(api_key=key, base_url='https://api.deepseek.com', model='deepseek-chat', max_retries=0)
    try:
        response = await client.complete(LLMRequest(system='Return a JSON object with ok=true.', messages=[ChatMessage(role='user', content='Connectivity check. Respond {"ok":true}.')], json_mode=True, max_output_tokens=32, timeout_seconds=15, operation_key='real-connectivity'))
        print(json.dumps({'connectivity':'ok', 'model':response.model,'text':response.text,'latency_ms':response.latency_ms,'tokens':response.usage.total_tokens}),flush=True)
    except Exception as exc:
        print(json.dumps({'connectivity':'failed','code':getattr(exc,'code',type(exc).__name__),'message':str(exc).replace(key,'[REDACTED]')},ensure_ascii=False),flush=True)
        await client.aclose()
        raise SystemExit(1)
    if '--connect-only' in sys.argv:
        await client.aclose()
        return
    await client.aclose()
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    data_dir = BACKEND.parent/'data'/('real-model-'+stamp)
    os.environ['HW2_DATA_DIR'] = str(data_dir)
    from app.main import create_app
    from app.settings import Settings
    from httpx import AsyncClient, ASGITransport
    settings = Settings(_env_file=None, data_dir=data_dir, model_api_key=key, log_level='warning')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    calls = []
    class MeasuredDeepSeekClient(DeepSeekClient):
        async def complete(self, request):
            response = await super().complete(request)
            calls.append({'operation_key': request.operation_key, 'model': response.model, 'latency_ms': response.latency_ms, 'prompt_tokens': response.usage.prompt_tokens, 'completion_tokens': response.usage.completion_tokens, 'total_tokens': response.usage.total_tokens})
            return response
    app = create_app(settings, llm_factory=lambda: MeasuredDeepSeekClient(api_key=key, base_url=settings.model_base_url, model=settings.model_name, timeout_seconds=settings.model_timeout_seconds, max_retries=settings.max_model_retries))
    svc = app.state.services
    digest = hashlib.sha256()
    for source in sorted((BACKEND/'app').rglob('*')):
        if source.suffix in {'.py', '.txt', '.sql'}:
            digest.update(str(source.relative_to(BACKEND)).encode())
            digest.update(source.read_bytes())
    summary = {'backend_source_sha256':digest.hexdigest(), 'tested_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'provider':'deepseek','model':'deepseek-chat','data_dir':str(data_dir),'connectivity':'ok','cases':[]}
    def save():
        summary['model_calls'] = list(calls)
        (data_dir/'real-model-results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2).replace(key,'[REDACTED]'))
    examples = BACKEND.parent/'examples'
    scenarios = [
      ('clean','clean','sequential','检查 stats.py 的语法、可变默认参数和吞异常静态问题。mean/median 空输入按接口约定抛出 ValueError，属于正确行为。无需修改正确代码。首次审查全部通过时结束并跳过修复和验证。'),
      ('repairable','repairable','sequential','审查并修复 helpers.py：add_item 每次省略 items 参数时应使用独立列表，显式传入列表时追加到该列表；average([]) 必须抛 ValueError，非空列表返回算术平均；parse_port 仅接受整数文本且范围为 1..65535，空字符串、非整数、越界均抛 ValueError。必须生成真实行为测试验证，不能只做语法检查。'),
      ('multi_file','multi_file','sequential','审查 calculator 包并修复 clamp(value,low,high)：low>high 时交换边界，超下界返回 low，超上界返回 high，范围内返回 value。safe_divide 除零返回 None；format_ratio 除零返回 n/a、正常格式两位小数；describe_score 分数先截断到 0..100 再分类，101 应为 excellent。生成行为测试证明缺陷并验证修复。'),
      ('parallel','repairable','parallel','检查并修复 helpers.py 的可变默认参数 add_item，省略 items 时每次使用独立列表，保留显式传入列表的追加行为；针对这个目标建立必需静态规则 B006-mutable-default。修改后在同一源码版本并行复审和验证，并收齐两个分支后再结束。其他功能不在本次目标范围内。'),
      ('parallel_multi_file','multi_file','parallel','审查并修复 calculator/core.py 的 clamp(value,low,high)：low>high 时交换边界，超下界返回 low，超上界返回 high，范围内返回 value；calculator/helpers.py 的 describe_score 应先截断到 0..100 再分类，101 应为 excellent。生成同一组真实行为测试，在原版本证明缺陷，在修改后版本证明修复并检查已有正确行为。修改后同一源码版本并行复审和验证，两个分支都回报后才能结束；并行和收齐要求写入 execution_requirements，不能作为源码检查项。'),
    ]
    if '--cases' in sys.argv:
        selected = set(sys.argv[sys.argv.index('--cases') + 1].split(','))
        unknown = selected - {case[0] for case in scenarios}
        if unknown:
            raise ValueError('Unknown smoke cases: ' + ','.join(sorted(unknown)))
        scenarios = [case for case in scenarios if case[0] in selected]
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as http:
        templates=(await http.get('/api/workflows/templates')).json()
        workflows={}
        for mode in ['sequential','parallel']:
            template=next(t for t in templates if t['check_mode']==mode)
            saved=await http.post('/api/workflows',json=template['config'])
            saved.raise_for_status()
            workflows[mode]=saved.json()['workflow_version']
        for name,folder,mode,goal in scenarios:
            files=[('files',(str(p.relative_to(examples/folder)),p.read_bytes(),'text/x-python')) for p in sorted((examples/folder).rglob('*.py'))]
            up=await http.post('/api/sources',files=files)
            up.raise_for_status()
            sub=await http.post('/api/tasks',json={'source_id':up.json()['source_id'],'goal':goal,'workflow_version':workflows[mode],'check_mode':mode},headers={'Idempotency-Key':'real-'+stamp+'-'+name})
            sub.raise_for_status()
            root_id=sub.json()['root_task_id']
            print(json.dumps({'case':name,'started':root_id,'mode':mode},ensure_ascii=False),flush=True)
            started=time.monotonic()
            last_seq=0
            while svc.runner.is_running(root_id):
                await asyncio.sleep(5)
                events=svc.repos.events.list_after(root_id,last_seq,limit=1000)
                if events:
                    last_seq=events[-1].sequence
                    meaningful=[e for e in events if e.event_type.value in {'parent_decided','task_dispatched','result_received','waiting_recovery','task_completed','action_rejected','tool_finished','detection_completed'}]
                    if meaningful:
                        e=meaningful[-1]
                        print(json.dumps({'case':name,'elapsed_s':round(time.monotonic()-started),'event':e.event_type.value,'actor':e.actor_id,'summary':str(e.payload)[:320]},ensure_ascii=False).replace(key,'[REDACTED]'),flush=True)
                if time.monotonic()-started>900:
                    print(json.dumps({'case':name,'stopped':'outer 900s deadline'}),flush=True)
                    await svc.runner.shutdown()
                    break
            detail=(await http.get('/api/tasks/'+root_id)).json()
            attempts=(await http.get('/api/tasks/'+root_id+'/attempts')).json()
            events=[e.model_dump(mode='json') for e in svc.repos.events.list_after(root_id,0,limit=10000)]
            artifacts=(await http.get('/api/tasks/'+root_id+'/artifacts')).json()
            batches=svc.repos.batches.list_by_root(root_id)
            result={'case':name,'root_task_id':root_id,'status':detail.get('status'),'passed':detail.get('passed'),'elapsed_s':round(time.monotonic()-started),'required_action':detail.get('required_action'),'error':detail.get('error'),'checks':detail.get('checks'),'attempts':[{'task_kind':a['task_kind'],'attempt_no':a['attempt_no'],'status':a['status'],'source_version':a['source_version'],'result_summary':(a.get('result') or {}).get('summary')} for a in attempts],'batches':batches,'artifact_count':len(artifacts)}
            summary['cases'].append(result)
            for suffix,body in [('detail',detail),('attempts',attempts),('events',events),('artifacts',artifacts)]:
                (data_dir/(name+'-'+suffix+'.json')).write_text(json.dumps(body,ensure_ascii=False,indent=2,default=str).replace(key,'[REDACTED]'))
            report=await http.get('/api/tasks/'+root_id+'/report')
            if report.status_code==200:
                (data_dir/(name+'-report.json')).write_text(json.dumps(report.json(),ensure_ascii=False,indent=2).replace(key,'[REDACTED]'))
            save()
            print(json.dumps({'case':name,'status':result['status'],'passed':result['passed'],'required_action':result['required_action'],'elapsed_s':result['elapsed_s']},ensure_ascii=False).replace(key,'[REDACTED]'),flush=True)
    await svc.runner.shutdown()
    save()
    print('RESULTS '+str(data_dir/'real-model-results.json'),flush=True)
    if any(case['status'] != 'completed' or case['passed'] is not True for case in summary['cases']):
        raise SystemExit(1)

asyncio.run(main())
