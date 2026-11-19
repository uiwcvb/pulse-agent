"""Pairing-protected cloud-computer routes and durable delivery into a Bot's chat."""
from __future__ import annotations

import asyncio, contextlib, re
from typing import Any
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError

from assistants import get_assistant
from computer_client import ComputerClient, ComputerUnavailable
from computer_files import UploadChunk, UploadSimple, UploadStart
from computer_models import InputAction, NavigateInput, SteerInput, UserComputerTask


class ComputerCoordinator:
    def __init__(self, store, brain, publish, client=None):
        self.store = store
        self.brain = brain
        self.publish = publish
        self.client = client or ComputerClient(config_getter=lambda: store.config, data_dir=getattr(store, 'data_dir', None))
        self.monitor = None
        self.run_lock = asyncio.Lock()

    async def run(self, data: UserComputerTask):
        async with self.run_lock:
            try:
                bot = get_assistant(self.store, data.bot_id)
            except ValueError as error:
                raise HTTPException(404, str(error)) from None
            cfg = self.store.config
            base_url = cfg.effective_base_url()
            api_key = cfg.effective_api_key(big=True) or cfg.effective_api_key(big=False)
            model_name = cfg.effective_model(bot_model=bot.get('model', ''), big=True) or cfg.effective_model(bot_model=bot.get('model', ''), big=False)
            if not api_key or not model_name:
                raise HTTPException(409, '先配置执行模型，或接管云电脑自己操作')
            previous = self.store.state.get('computer_job')
            if previous and not previous.get('reported'):
                settled = await self.settle(previous, await self.client.status(force=True))
                if not settled and not previous.get('reported'):
                    with contextlib.suppress(Exception):
                        await self.client.call('POST', '/stop')
                    previous['reported'] = True
                    self.store.save()
            result = await self.client.call('POST', '/run', {'goal': data.goal, 'base_url': base_url, 'api_key': api_key, 'model': model_name, 'max_steps': data.max_steps})
            job = result.get('job')
            if not job or not job.get('id'):
                raise ComputerUnavailable('云电脑没有返回任务编号')
            self.store.message('user', data.goal, 'chat', bot_id=data.bot_id)
            self.store.state['computer_job'] = {'id': job['id'], 'bot_id': data.bot_id, 'goal': data.goal, 'reported': False, 'question_reported': ''}
            self.store.save()
            self.ensure_monitor()
            return {'started': True, 'job': job, 'bot_id': bot['id']}

    def ensure_monitor(self):
        saved = self.store.state.get('computer_job')
        if saved and not saved.get('reported') and (self.monitor is None or self.monitor.done()):
            self.monitor = asyncio.create_task(self.watch(saved['id']))

    async def watch(self, job_id):
        while True:
            await asyncio.sleep(1.5)
            saved = self.store.state.get('computer_job', {})
            if saved.get('id') != job_id or saved.get('reported'):
                return
            status = await self.client.status(force=True)
            job = status.get('job')
            if not status.get('available'):
                continue
            if job and job.get('id') == job_id:
                question = job.get('question', '')
                if status.get('control') == 'user' and question and question != saved.get('question_reported'):
                    await self.deliver('云电脑需要你接管：' + question, 'computer_question', saved, job)
                    saved['question_reported'] = question
                    self.store.save()
            if await self.settle(saved, status):
                return

    async def settle(self, saved, status):
        if saved.get('reported') or not status.get('available'):
            return False
        job = status.get('job')
        if not job or job.get('id') != saved['id']:
            saved['reported'] = True
            self.store.save()
            return True
        if job.get('state') not in ('done', 'failed', 'stopped', 'interrupted'):
            return False
        text = job.get('result') or '本轮结束，请查看云电脑和任务文件。'
        kind = 'computer_result' if job.get('state') == 'done' else 'stopped' if job.get('state') == 'stopped' else 'error'
        await self.deliver(text, kind, saved, job or {})
        saved['reported'] = True
        self.store.save()
        return True

    async def deliver(self, text, kind, saved, job):
        try:
            bot = get_assistant(self.store, saved['bot_id'])
        except ValueError:
            return
        await self.brain.emit(
            text, kind, track=True, bot_id=bot['id'], notify=bot.get('notify', True),
            computer_job=saved['id'], goal=saved['goal'], computer_trace=job.get('trace', []), engine='cloud_computer',
        )

    async def close(self):
        if self.monitor and not self.monitor.done():
            self.monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor
        if self.client and hasattr(self.client, 'aclose'):
            with contextlib.suppress(Exception):
                await self.client.aclose()


_FILE_EXTS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.c', '.cpp', '.cc', '.h', '.hpp', '.java', '.kt', '.go', '.rs',
    '.sh', '.bash', '.zsh', '.ps1', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.env',
    '.txt', '.md', '.csv', '.tsv', '.xlsx', '.xls', '.docx', '.doc', '.pdf', '.png', '.jpg', '.jpeg',
    '.gif', '.svg', '.webp', '.zip', '.tar', '.gz', '.log', '.sql', '.db', '.sqlite', '.html', '.htm', '.css',
}


def _looks_like_web_url(val: str) -> bool:
    s = (val or '').strip()
    low = s.lower()
    if low.startswith(('http://', 'https://')):
        return True
    if '://' in low or ' ' in s or s.startswith(('/', './', '../', '~')):
        return False
    if '.' not in s:
        return False
    host_part = s.split('/', 1)[0].split(':', 1)[0]
    if '/' not in s and any(low.endswith(ext) for ext in _FILE_EXTS):
        return False
    tld = host_part.rsplit('.', 1)[-1].lower()
    if tld.isdigit() and host_part.count('.') == 3:
        return True
    return tld.isalpha() and 2 <= len(tld) <= 12


def _with_frame_aliases(res: Any) -> Any:
    if isinstance(res, dict):
        if res.get('error') and 'ok' not in res:
            res['ok'] = False
        res.setdefault('ok', True)
        img = res.get('image')
        if img:
            res.setdefault('screenshot', img)
            res.setdefault('frame', img)
        res.setdefault('control', 'agent')
        res.setdefault('user_takeover_locked', False)
        res.setdefault('url', '')
        res.setdefault('active_window', None)
    return res


def install_computer_routes(app, auth, coordinator):
    router = APIRouter(prefix='/api/computer', dependencies=[Depends(auth)])

    async def forward(method, path, body=None, query=None):
        try:
            return await coordinator.client.call(method, path, body, query)
        except ComputerUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.get('/status')
    @router.get('/state')
    async def status():
        coordinator.ensure_monitor()
        result = await coordinator.client.status()
        cfg = coordinator.store.config
        result['ok'] = bool(result.get('available', False))
        result['model_ready'] = bool((cfg.effective_api_key(big=True) or cfg.effective_api_key(big=False)) and (cfg.effective_model(big=True) or cfg.effective_model(big=False)))
        result.setdefault('user_takeover_locked', False)
        result.setdefault('url', '')
        result.setdefault('active_window', None)
        saved = coordinator.store.state.get('computer_job', {})
        if (result.get('job') or {}).get('id') == saved.get('id'):
            result['bot_id'] = saved.get('bot_id')
        return result

    @router.get('/frame')
    @router.get('/screenshot')
    async def frame(since: str = '', quality: int = 62):
        q = {'since': since, 'quality': max(30, min(90, quality))} if since else {'quality': max(30, min(90, quality))}
        res = await forward('GET', '/frame', query=q)
        return _with_frame_aliases(res)

    @router.post('/screenshot')
    async def post_screenshot(payload: dict[str, Any] | None = Body(default=None)):
        body = payload or {}
        q_val = int(body.get('quality') or 62)
        since_val = str(body.get('since') or '')
        return await frame(since=since_val, quality=q_val)

    @router.post('/takeover')
    async def takeover():
        return _with_frame_aliases(await forward('POST', '/takeover'))

    @router.post('/handback')
    async def handback():
        return _with_frame_aliases(await forward('POST', '/handback'))

    @router.post('/stop')
    async def stop():
        return _with_frame_aliases(await forward('POST', '/stop'))

    @router.post('/navigate')
    async def navigate(data: NavigateInput):
        return _with_frame_aliases(await forward('POST', '/navigate', data.model_dump()))

    @router.post('/search')
    async def search_web_desktop(payload: dict[str, Any]):
        raw = dict(payload or {})
        q = str(raw.get('query') or raw.get('text') or '').strip()
        if not q:
            raise HTTPException(422, '搜索词不能为空')
        res = await forward('POST', '/search', {'query': q, 'engine': str(raw.get('engine') or 'bing'), 'include_frame': bool(raw.get('include_frame', True))})
        return _with_frame_aliases(res)

    @router.post('/input')
    async def input_action(payload: dict[str, Any]):
        raw = dict(payload or {})
        if 'kind' not in raw and ('action' in raw or 'op' in raw):
            raw['kind'] = raw.pop('action', None) or raw.pop('op', None)
        alias_map = {
            'tap': 'click', 'press': 'key', 'press_key': 'key', 'type_text': 'type',
            'shell': 'bash', 'exec': 'bash', 'terminal': 'bash', 'close': 'close_window',
            'delay': 'wait', 'sleep': 'wait', 'app': 'launch_app', 'open_app': 'launch_app',
        }
        if isinstance(raw.get('kind'), str):
            k_low = raw['kind'].strip().lower()
            raw['kind'] = alias_map.get(k_low, k_low)
        ref_in = str(raw.get('ref') or '').strip()
        if raw.get('kind') == 'click' and ref_in.startswith('@b'):
            raw['kind'] = 'click_ref'
        elif raw.get('kind') == 'type' and ref_in.startswith('@b'):
            raw['kind'] = 'type_ref'
        elif raw.get('kind') in ('click_text', 'tap_node', 'click_node'):
            raw['kind'] = 'find_text'
            raw['click'] = True
        if raw.get('kind') == 'launch_app':
            raw['app'] = str(raw.get('app') or raw.get('target') or raw.get('name') or 'browser').strip()
            if raw.get('url') and not raw.get('text'):
                raw['text'] = str(raw['url'])
        if 'clear' not in raw and 'clear_first' in raw:
            raw['clear'] = bool(raw.pop('clear_first'))
        if 'submit' in raw:
            raw['submit'] = bool(raw['submit'])
        raw.setdefault('include_frame', True)
        try:
            data = InputAction.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None
        res = await forward('POST', '/input', data.model_dump())
        return _with_frame_aliases(res)

    @router.post('/action')
    async def computer_action(payload: dict[str, Any]):
        raw = dict(payload or {})
        op = str(raw.get('op') or raw.get('action') or raw.get('kind') or 'status').strip().lower()
        if op in ('status', 'state'):
            return await status()
        if op in ('screenshot', 'frame', 'capture', 'snapshot', 'browser_snapshot', 'ui_tree', 'read_screen'):
            return await frame(since=str(raw.get('since') or ''), quality=int(raw.get('quality') or 62))
        if op == 'takeover':
            return await takeover()
        if op in ('handback', 'release'):
            return await handback()
        if op == 'stop':
            return await stop()
        if op in ('navigate', 'open_url'):
            return await navigate(NavigateInput(url=str(raw.get('url') or raw.get('target') or ''), include_frame=bool(raw.get('include_frame', True))))
        if op in ('search', 'web_search'):
            return await search_web_desktop(raw)
        if op == 'open':
            target_val = str(raw.get('url') or raw.get('uri') or raw.get('target') or raw.get('app') or raw.get('text') or '').strip()
            low_tv = target_val.lower()
            if ('://' in low_tv or (re.match(r'^[a-z][a-z0-9+.-]+:', low_tv) and not re.match(r'^[^/:]+:\d+(?:[/?#]|$)', low_tv))) and not low_tv.startswith(('http://', 'https://')):
                raise HTTPException(400, '仅支持打开 http:// 或 https:// 网页地址')
            if _looks_like_web_url(target_val):
                url_val = target_val if target_val.lower().startswith(('http://', 'https://')) else ('https://' + target_val)
                return await navigate(NavigateInput(url=url_val, include_frame=bool(raw.get('include_frame', True))))
            return await input_action({**raw, 'kind': 'launch_app', 'app': raw.get('app') or ('editor' if any(target_val.lower().endswith(e) for e in _FILE_EXTS) else target_val) or 'browser', 'text': target_val if any(target_val.lower().endswith(e) for e in _FILE_EXTS) else str(raw.get('text') or '')})
        if op in ('files', 'list_files'):
            return await forward('GET', '/files')
        if op in ('pull_file', 'push_file', 'send_screenshot', 'share_image', 'send_qr', 'send_image', 'zoom', 'crop', 'magnify'):
            if coordinator.brain is not None and hasattr(coordinator.brain, '_tool_cloud_desktop'):
                res = await coordinator.brain._tool_cloud_desktop({**raw, 'op': op})
                if isinstance(res, dict):
                    return res
        return await input_action({**raw, 'kind': op})

    @router.post('/steer')
    async def steer(data: SteerInput):
        return await forward('POST', '/steer', data.model_dump())

    @router.post('/run')
    async def run(data: UserComputerTask):
        try:
            return await coordinator.run(data)
        except ComputerUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.get('/files')
    async def files():
        return await forward('GET', '/files')

    @router.get('/file')
    async def read_file(path: str, offset: int = 0, size: int = 262144, version: str | None = None):
        if offset < 0 or not 1 <= size <= 524288:
            raise HTTPException(400, '文件分块参数无效')
        query = {'path': path, 'offset': offset, 'size': size}
        if version:
            query['version'] = version
        return await forward('GET', '/file', query=query)

    @router.post('/upload')
    async def simple_upload(data: UploadSimple):
        return await forward('POST', '/library/upload', data.model_dump())

    @router.post('/uploads')
    async def start_upload(data: UploadStart):
        return await forward('POST', '/library/uploads', data.model_dump())

    @router.put('/uploads/{key}')
    async def upload_chunk(key: str, data: UploadChunk):
        return await forward('PUT', '/library/uploads/' + key, data.model_dump())

    @router.post('/uploads/{key}/finish')
    async def finish_upload(key: str):
        return await forward('POST', '/library/uploads/' + key + '/finish')

    @router.delete('/uploads/{key}')
    async def cancel_upload(key: str):
        return await forward('DELETE', '/library/uploads/' + key)

    app.include_router(router)
