"""Task file library and resumable transfers, confined to this application's files."""
from __future__ import annotations
import asyncio,base64,inspect,logging,os,re,secrets,threading,time
from pathlib import Path
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field

class UploadStart(BaseModel):
    name:str=Field(min_length=1,max_length=180)
    size:int=Field(ge=0,le=50_000_000)
    sha256:str=Field(default='',max_length=128)
class UploadChunk(BaseModel):
    offset:int=Field(ge=0,le=50_000_000)
    data:str=Field(max_length=700000)
class UploadSimple(BaseModel):
    name:str=Field(min_length=1,max_length=180)
    data:str=Field(default='',max_length=28_000_000)
    sha256:str=Field(default='',max_length=128)

class WorkspaceFiles:
    def __init__(self,workspace:Path,transfers:Path):
        self.root=workspace.resolve();self.transfers=transfers
        self.pending={};self.lock=threading.RLock()
    def path(self,name):
        if not name.startswith('workspace/'):raise ValueError('文件不在任务工作区')
        relative=name[len('workspace/'):]
        parts=relative.split('/')
        if not relative or len(relative)>240 or relative in ('settings.json','bots.json','routines.json','SYSTEM.md') or any(not p or p.startswith('.') for p in parts) or '\\' in relative or ':' in relative:raise ValueError('文件路径无效')
        path=self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(self.root)):raise ValueError('文件路径无效')
        return path
    def resolve_existing_path(self,raw_name:str)->Path:
        norm=str(raw_name or '').strip().replace('\\','/')
        if norm.startswith('uploads/'):norm='workspace/'+norm
        cand=self.path(norm)
        if cand.is_file():return cand
        if norm.startswith('workspace/uploads/'):
            tail=norm[len('workspace/uploads/'):]
            if '/' not in tail and tail:
                clean=re.sub(r'^[0-9a-f]{10}-','',tail)
                if clean and clean!=tail:
                    alt=self.path('workspace/uploads/'+clean)
                    if alt.is_file():return alt
                uploads_dir=self.path('workspace/uploads')
                if uploads_dir.is_dir():
                    matches=[f for f in uploads_dir.glob('*-'+tail) if f.is_file() and re.match(r'^[0-9a-f]{10}-',f.name)]
                    if matches:
                        matches.sort(key=lambda p:p.stat().st_mtime,reverse=True)
                        return matches[0]
        return cand
    def files(self):
        result=[]
        for file in self.root.rglob('*'):
            relative=file.relative_to(self.root).as_posix()
            if any(p.startswith('.') for p in relative.split('/')) or file.is_symlink() or not file.is_file():continue
            try:
                file=self.path('workspace/'+relative);stat=file.stat()
            except (OSError,ValueError):continue
            result.append({'path':'workspace/'+relative,'bytes':stat.st_size,'modified_at':stat.st_mtime})
            if len(result)>=1000:break
        return sorted(result,key=lambda f:f['modified_at'],reverse=True)
    def read(self,name,offset,size,version=None):
        if offset<0 or not 1<=size<=524288:raise ValueError('分块参数无效')
        path=self.resolve_existing_path(name);stat=path.stat();fingerprint=f'{stat.st_mtime_ns}-{stat.st_size}'
        if not path.is_file() or stat.st_size>50_000_000 or offset>stat.st_size:raise ValueError('文件过大或位置无效')
        if version and fingerprint!=version:raise ValueError('文件在下载中发生了变化，请重新下载')
        with path.open('rb') as f:f.seek(offset);data=f.read(size)
        return {'path':name,'size':stat.st_size,'offset':offset,'next_offset':offset+len(data),'eof':offset+len(data)==stat.st_size,'data':base64.b64encode(data).decode(),'version':fingerprint}
    def start(self,data:UploadStart):
        if '/' in data.name or '\\' in data.name or ':' in data.name or data.name.startswith('.') or any(ord(c)<32 for c in data.name):raise ValueError('文件名无效')
        with self.lock:
            self.root.mkdir(parents=True,exist_ok=True);self.transfers.mkdir(parents=True,exist_ok=True)
            # Only our abandoned partial transfers are reclaimed.
            for file in self.transfers.glob('*.partial'):
                if time.time()-file.stat().st_mtime>86400:file.unlink(missing_ok=True)
            expired=[key for key,v in self.pending.items() if time.time()-v['created']>86400]
            for key in expired:self.cancel(key)
            if len(self.pending)>=10:raise ValueError('等待中的上传太多，请完成或取消现有上传')
            sha=str(data.sha256 or '').strip()
            safe_name=Path(str(data.name or '').replace('\\','/')).name.strip()
            if not safe_name or safe_name in ('.','..'):raise ValueError('文件名无效')
            key=secrets.token_hex(16);partial=self.transfers/(key+'.partial');partial.touch(exist_ok=False)
            self.pending[key]={'name':safe_name,'size':data.size,'sha256':sha,'offset':0,'created':time.time(),'partial':partial}
            return {'id':key,'key':key,'offset':0}
    def chunk(self,key,data:UploadChunk):
        with self.lock:
            item=self.pending.get(key)
            if not item:raise ValueError('上传已过期，请重新选择文件')
            payload=base64.b64decode(data.data,validate=True)
            if not payload or len(payload)>524288 or data.offset!=item['offset'] or item['offset']+len(payload)>item['size']:raise ValueError('上传分块不连续或超过文件大小')
            with item['partial'].open('ab') as f:f.write(payload)
            item['offset']+=len(payload)
            return {'id':key,'key':key,'offset':item['offset']}
    def finish(self,key):
        with self.lock:
            item=self.pending.get(key)
            if not item:raise ValueError('上传不存在')
            if item['offset']!=item['size']:raise ValueError('文件尚未传完')
            if item['partial'].stat().st_size!=item['size']:
                self.cancel(key);raise ValueError('文件大小不一致，上传已丢弃')
            relative='workspace/uploads/'+item['name'];target=self.path(relative);target.parent.mkdir(parents=True,exist_ok=True)
            item['partial'].replace(target);self.pending.pop(key)
            return {'path':relative,'name':item['name'],'bytes':item['size'],'sha256':item.get('sha256','')}
    def put_simple(self,data:UploadSimple):
        safe_name=Path(str(data.name or '').replace('\\','/')).name.strip()
        if not safe_name or safe_name in ('.','..'):raise ValueError('文件名无效')
        raw=base64.b64decode(data.data,validate=True) if data.data else b''
        if len(raw)>20_000_000:raise ValueError('单次上传文件不能超过 20 MB')
        sha=str(data.sha256 or '').strip()
        relative='workspace/uploads/'+safe_name
        with self.lock:
            self.root.mkdir(parents=True,exist_ok=True);self.transfers.mkdir(parents=True,exist_ok=True)
            target=self.path(relative);target.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.transfers/(secrets.token_hex(12)+'.tmp');tmp.write_bytes(raw);tmp.replace(target)
        return {'path':relative,'name':safe_name,'bytes':len(raw),'sha256':sha}
    def cancel(self,key):
        with self.lock:
            item=self.pending.pop(key,None)
            if item:item['partial'].unlink(missing_ok=True)
        return {'cancelled':True}

async def mirror_upload_across_nodes(result:dict,*,workspace_root:Path,computer=None,dispatch_dsh=None,publish=None):
    rel=str((result or {}).get('path') or '').strip()
    if not rel.startswith('workspace/'):return
    sub=rel[len('workspace/'):]
    local_file=(workspace_root/sub).resolve()
    try:
        if not local_file.is_file() or not local_file.is_relative_to(workspace_root.resolve()):return
    except OSError:return
    clean_name=str(result.get('name') or re.sub(r'^[0-9a-f]{10}-','',local_file.name) or local_file.name)
    file_bytes=int(result.get('bytes') or local_file.stat().st_size)
    sha=str(result.get('sha256') or '')
    if computer is not None and getattr(computer,'client',None) is not None and getattr(computer.client,'configured',False):
        try:await computer.client.upload_file(local_file,remote_name=clean_name)
        except Exception:pass
    if dispatch_dsh is not None:
        try:
            payload={'op':'sync_uploaded_file','workspace_path':'uploads/'+clean_name,'name':clean_name,'bytes':file_bytes,'sha256':sha}
            if file_bytes<=1_500_000:
                payload['data_b64']=base64.b64encode(local_file.read_bytes()).decode('ascii')
            res=dispatch_dsh(payload)
            if inspect.isawaitable(res):await res
        except Exception:pass
    if publish is not None:
        try:
            payload_ev={'type':'file.uploaded','path':rel,'workspace_path':'uploads/'+clean_name,'name':clean_name,'bytes':file_bytes,'sha256':sha}
            pos_params=[
                p for p in inspect.signature(publish).parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if len(pos_params)>=2 and pos_params[1].default is inspect.Parameter.empty:
                res=publish('file.uploaded',payload_ev)
            else:
                res=publish(payload_ev)
            if inspect.isawaitable(res):await res
        except Exception as exc:
            logging.getLogger(__name__).warning('mirror_upload publish failed: %s', exc)

def install_file_routes(app,auth,files:WorkspaceFiles,prefix='/api/files',on_uploaded=None):
    router=APIRouter(prefix=prefix,dependencies=[Depends(auth)])
    def checked(fn,*args,**kw):
        try:return fn(*args,**kw)
        except FileNotFoundError:raise HTTPException(404,'文件不存在') from None
        except (ValueError,OSError):raise HTTPException(409,'文件路径或传输状态无效，请刷新后重试') from None
    async def notify_uploaded(res:dict):
        if on_uploaded is None:return
        try:
            out=on_uploaded(res)
            if inspect.isawaitable(out):await out
        except Exception:pass
    @router.get('')
    async def listing():return {'files':files.files()}
    @router.get('/file')
    async def read(path:str,offset:int=0,size:int=262144,version:str|None=None):return checked(files.read,path,offset,size,version)
    @router.post('/upload')
    async def simple_upload(data:UploadSimple):
        res=checked(files.put_simple,data)
        await notify_uploaded(res)
        return res
    @router.post('/uploads')
    async def start(data:UploadStart):return checked(files.start,data)
    @router.put('/uploads/{key}')
    async def chunk(key:str,data:UploadChunk):return checked(files.chunk,key,data)
    @router.post('/uploads/{key}/finish')
    async def finish(key:str):
        res=checked(files.finish,key)
        await notify_uploaded(res)
        return res
    @router.delete('/uploads/{key}')
    async def cancel(key:str):return checked(files.cancel,key)
    app.include_router(router)
