from __future__ import annotations
import asyncio, smtplib, ssl, time
from email.message import EmailMessage

class Escalation:
    def __init__(self,store,vault,publish):
        self.store,self.vault,self.publish=store,vault,publish
        self.mail_lock=asyncio.Lock()
        self.last_test=0

    async def track(self,message,demo=False):
        if not isinstance(message, dict) or message.get('silent') or not message.get('id'):
            return
        if not demo and message.get('kind') not in ('deadline', 'escalation_demo'):
            return
        msg_id = str(message['id'])
        msg_txt = str(message.get('text') or '')[:3000]
        with self.store.lock:
            s=self.store.state
            s.setdefault('pending', {})[msg_id]={'id':msg_id,'text':msg_txt,
                'created_at':time.time(),'stage':1,'last_email':0,'emails':0,'demo':demo,'email_status':'pending'}
            self.store.save()
        await self.publish({'type':'escalation','stage':1,'message':message,'id':msg_id,'demo':demo})

    async def enqueue(self,message,demo=True):
        return await self.track(message,demo=demo)

    async def ack(self,ids):
        with self.store.lock:
            pending = self.store.state.setdefault('pending', {})
            for mid in ids:
                pending.pop(mid, None)
            self.store.save()
        await self.publish({'type':'ack','ids':ids})

    async def snooze(self,minutes=60):
        with self.store.lock:
            self.store.state['snooze_until']=time.time()+minutes*60
            self.store.save()
            until_ts = self.store.state['snooze_until']
        from vault_manager import now_iso
        description=f'免打扰 {minutes} 分钟' if minutes else '好奇心开启'
        self.vault.write('profile/annoyance_valve.md',f'# 烦人度阀门\n\n状态：{description}\n\n设置于：{now_iso()}\n\n结束时间戳：{until_ts}\n')
        self.vault.log(f'烦人度阀门：{description}。')
        await self.publish({'type':'snooze','until':until_ts})
        return {'snooze_until':until_ts}

    def _mail(self,text,test=False):
        c=self.store.config.smtp
        if not c.host or not c.recipient or not (c.sender or c.username):
            return {'sent':False,'reason':'尚未配置 SMTP 主机、收件邮箱和发件地址'}
        msg=EmailMessage()
        msg['Subject']=('【DSB · DeepSeek Bot · 测试】' if test else '【DSB · DeepSeek Bot · 等你拍板】')+'替你准备的成果还没看呢'
        msg['From']=c.sender or c.username
        msg['To']=c.recipient
        msg.set_content(f'DSB · DeepSeek Bot 待办提醒。\n\n{text}\n\n打开 DSB 查看、回复，或选择「别吵我 1 小时」即可停止催单。\n本项目每个待办最多发送 3 封催单邮件。')
        context=ssl.create_default_context()
        cls=smtplib.SMTP_SSL if c.security=='ssl' else smtplib.SMTP
        kwargs={'context':context} if c.security=='ssl' else {}
        with cls(c.host,c.port,timeout=20,**kwargs) as smtp:
            if c.security=='starttls': smtp.starttls(context=context)
            if c.username: smtp.login(c.username,c.password)
            smtp.send_message(msg)
        return {'sent':True}

    async def mail(self,text,test=False):
        async with self.mail_lock:
            if test and time.time()-self.last_test<30: return {'sent':False,'reason':'测试邮件间隔至少 30 秒'}
            if test: self.last_test=time.time()
            try: return await asyncio.to_thread(self._mail,text,test)
            except Exception as e:
                # Do not expose SMTP credentials or provider response bodies.
                return {'sent':False,'reason':f'SMTP 发送失败：{type(e).__name__}，请核对地址和授权码'}

    async def tick(self, now=None):
        now = float(now if now is not None else time.time())
        if float(self.store.state.get('snooze_until') or 0.0) > now:
            return
        pending = self.store.state.get('pending')
        if not isinstance(pending, dict) or not pending:
            return
        c = self.store.config
        pruned = False
        for pid, p in list(pending.items()):
            if not isinstance(p, dict):
                pending.pop(pid, None)
                pruned = True
                continue
            status = str(p.get('status') or '').lower()
            created_at = float(p.get('created_at') or now)
            age = max(0.0, now - created_at)
            if p.get('acked') or status in ('acked', 'done', 'resolved', 'cancelled', 'expired'):
                pending.pop(pid, None)
                pruned = True
                continue
            is_overlay_notif = str(pid).startswith('notif-') or p.get('email_status') == 'overlay_tool'
            if is_overlay_notif:
                if age > 72 * 3600.0:
                    pending.pop(pid, None)
                    pruned = True
                continue
            # Expire abandoned non-demo items after 7 days once escalated
            if not p.get('demo') and age > 7 * 86400.0 and int(p.get('stage') or 1) >= 3:
                pending.pop(pid, None)
                pruned = True
                continue
            p_id = str(p.get('id') or pid)
            is_demo = bool(p.get('demo'))
            t2, t3 = (4, 8) if is_demo else (c.email_after_minutes * 60, c.overlay_after_minutes * 60)
            emails_cnt = int(p.get('emails') or 0)
            last_email = float(p.get('last_email') or 0.0)
            stage = int(p.get('stage') or 1)
            text = str(p.get('text') or '')
            if age >= t2 and emails_cnt < 3 and (not last_email or (now - last_email) >= 900):
                result = await self.mail(text, test=is_demo)
                if p_id not in pending or float(self.store.state.get('snooze_until') or 0.0) > time.time():
                    continue
                p['emails'] = emails_cnt + 1
                p['last_email'] = now
                p['email_status'] = 'sent' if result['sent'] else result['reason']
                p['stage'] = max(2, stage)
                stage = p['stage']
                self.store.save()
                await self.publish({'type': 'escalation', 'stage': 2, 'id': p_id, 'text': text, 'email': result, 'demo': is_demo})
            if age >= t3 and stage < 3:
                if p_id not in pending:
                    continue
                p['stage'] = 3
                self.store.save()
                await self.publish({'type': 'escalation', 'stage': 3, 'id': p_id, 'text': text, 'demo': is_demo})
        if pruned:
            self.store.save()
