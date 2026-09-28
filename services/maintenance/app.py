"""Optional MCP service: inspect a read-only source tree and write change proposals.
No shell, deploy, test execution, arbitrary network access, or live source writes.
"""
import difflib
import hashlib
import hmac
import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
ROOT=Path(os.getenv('SOURCE_ROOT','/source'))
OUT=Path(os.getenv('PROPOSAL_ROOT','/proposals'))
TOKEN=os.getenv('MAINTENANCE_TOKEN','')
ALLOWED_ROOTS={'app','tests','docs','scripts','services','deployment'}
ALLOWED_FILES={'README.md','requirements.txt','requirements-dev.txt','Dockerfile','compose.yaml','compose.local.yaml','Caddyfile','package.json'}
EXT={'.py','.js','.cjs','.css','.html','.md','.txt','.yaml','.yml','.json'}


def safe_file(name):
    if not isinstance(name,str) or len(name)>200 or '\\' in name:raise ValueError('Некорректный путь.')
    path=PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or any(p.startswith('.') for p in path.parts):raise ValueError('Путь запрещён.')
    if name not in ALLOWED_FILES and (not path.parts or path.parts[0] not in ALLOWED_ROOTS or path.suffix not in EXT):raise ValueError('Путь не входит в разрешённые исходники.')
    target=ROOT.joinpath(*path.parts)
    cursor=ROOT
    for part in path.parts:
        cursor=cursor/part
        if cursor.is_symlink():raise ValueError('Символические ссылки запрещены.')
    if not target.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Путь вне исходников.')
    return target


def read_source(path):
    f=safe_file(path)
    if not f.is_file():return ''
    if f.stat().st_size>100000:raise ValueError('Файл превышает 100 КБ.')
    return f.read_text(encoding='utf-8')


def schema(properties,required):return {'type':'object','properties':properties,'required':required,'additionalProperties':False}
TEXT={'type':'string'}
TOOLS=[
 {'name':'source_list','description':'Список исходников Агентной ОС. Секреты, данные и скрытые файлы недоступны.','inputSchema':schema({},[])},
 {'name':'source_read','description':'Прочитать разрешённый исходник и его SHA-256.','inputSchema':schema({'path':TEXT},['path'])},
 {'name':'propose_change','description':'Сохранить новое содержимое файла как предложение в отдельном каталоге. Рабочий код не меняется.','inputSchema':schema({'path':TEXT,'expected_sha256':TEXT,'content':TEXT,'reason':TEXT},['path','expected_sha256','content','reason'])},
 {'name':'service_status','description':'Свободное место и состояние сервиса предложений. Это не доступ к Docker или root.','inputSchema':schema({},[])}]


def invoke(name,args):
    if name=='source_list':
        files=[]
        for folder in sorted(ALLOWED_ROOTS):
            base=ROOT/folder
            if not base.exists() or base.is_symlink():continue
            for f in base.rglob('*'):
                if len(files)>=1000:break
                try:
                    rel=f.relative_to(ROOT).as_posix();safe_file(rel)
                    if f.is_file() and f.stat().st_size<=100000:files.append(rel)
                except ValueError:pass
        files+=sorted(n for n in ALLOWED_FILES if (ROOT/n).is_file() and not (ROOT/n).is_symlink())
        return {'files':files}
    if name=='source_read':
        content=read_source(args['path'])
        return {'path':args['path'],'content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()}
    if name=='propose_change':
        content=args['content'];reason=args['reason']
        if not isinstance(content,str) or len(content.encode())>100000 or not isinstance(reason,str) or len(reason)>2000:raise ValueError('Слишком большое предложение.')
        old=read_source(args['path'])
        if hashlib.sha256(old.encode()).hexdigest()!=args['expected_sha256']:raise ValueError('Исходник изменился. Сначала прочитайте актуальный файл.')
        OUT.mkdir(parents=True,exist_ok=True)
        if len(list(OUT.glob('*.json')))>=200:raise ValueError('Лимит предложений достигнут. Владелец должен разобрать очередь.')
        id=uuid.uuid4().hex
        diff=''.join(difflib.unified_diff(old.splitlines(True),content.splitlines(True),fromfile='a/'+args['path'],tofile='b/'+args['path']))
        record={'id':id,'path':args['path'],'expected_sha256':args['expected_sha256'],'content':content,'reason':reason,'diff':diff,'status':'proposal_only'}
        file=OUT/(id+'.json')
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(record,stream,ensure_ascii=False)
        return {'id':id,'path':args['path'],'diff':diff,'status':'Предложение сохранено. Рабочая система не изменялась.'}
    if name=='service_status':
        usage=shutil.disk_usage(OUT if OUT.exists() else '/')
        return {'status':'ok','free_bytes':usage.free,'source_read_only':True,'proposal_count':len(list(OUT.glob('*.json'))) if OUT.exists() else 0,'shell':False}
    raise ValueError('Неизвестный инструмент.')


@app.post('/mcp')
async def mcp(request:Request):
    if len(TOKEN)<32:return JSONResponse({'error':'Сервис не настроен.'},503)
    if not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+TOKEN):return JSONResponse({'error':'Требуется авторизация.'},401)
    expected=os.getenv('MAINTENANCE_ORIGIN','')
    if request.headers.get('origin') and request.headers['origin']!=expected:return JSONResponse({'error':'Недопустимый Origin.'},403)
    chunks=[];size=0
    async for part in request.stream():
        size+=len(part)
        if size>250000:return JSONResponse({'error':'Слишком большой запрос.'},413)
        chunks.append(part)
    try:body=json.loads(b''.join(chunks))
    except Exception:return JSONResponse({'error':'Некорректный JSON.'},400)
    if not isinstance(body,dict):return JSONResponse({'error':'Некорректный запрос.'},400)
    method=body.get('method');params=body.get('params') or {};id=body.get('id')
    if method=='notifications/initialized':return Response(status_code=202)
    try:
        if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'agentnaya-maintenance','version':'2.0.0'}}
        elif method=='ping':result={}
        elif method=='tools/list':result={'tools':TOOLS}
        elif method=='tools/call':
            from jsonschema import validate
            tool=next(t for t in TOOLS if t['name']==params.get('name'))
            args=params.get('arguments',{});validate(args,tool['inputSchema'])
            result={'content':[{'type':'text','text':json.dumps(invoke(tool['name'],args),ensure_ascii=False)}]}
        else:return JSONResponse({'jsonrpc':'2.0','id':id,'error':{'code':-32601,'message':'Метод не поддерживается.'}})
    except Exception:return JSONResponse({'jsonrpc':'2.0','id':id,'error':{'code':-32602,'message':'Недопустимый запрос, путь или версия файла.'}})
    return JSONResponse({'jsonrpc':'2.0','id':id,'result':result})
