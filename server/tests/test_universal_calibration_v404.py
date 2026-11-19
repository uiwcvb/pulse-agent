import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from core.prompt_guards import (
    BAIKE_CALIBRATION_TAIL,
    BAIKE_TURN_CAL_NOTE,
    UNIVERSAL_ASSISTANT_CALIBRATION_TAIL,
    UNIVERSAL_TURN_CAL_NOTE,
    is_preachy_refusal,
    strip_response_calibration_blocks,
)
from core.turn_history import expand_concatenated_tool_calls, split_concatenated_json_objects
from main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "test-private-token")
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-private-token"
        client.post(
            "/api/config",
            json={
                "api_base_url": "https://api.openai.com/v1",
                "api_key": "sk-test-key",
                "default_model": "deepseek-v4.1-flash",
            },
        )
        yield app, client


def test_split_and_expand_concatenated_tool_call_arguments(env):
    raw_concat = (
        '{"op": "search", "query": "哈密 熊馨霖 三代人的脚印"}'
        '{"op": "search", "query": "哈密 柴雨淑 不同人的长征路"}'
    )
    objs = split_concatenated_json_objects(raw_concat)
    assert len(objs) == 2
    assert objs[0]["query"] == "哈密 熊馨霖 三代人的脚印"
    assert objs[1]["query"] == "哈密 柴雨淑 不同人的长征路"

    calls = [
        {
            "id": "call_multi_1",
            "type": "function",
            "function": {"name": "web", "arguments": raw_concat},
        }
    ]
    expanded = expand_concatenated_tool_calls(calls)
    assert len(expanded) == 2
    assert expanded[0]["id"] == "call_multi_1"
    assert expanded[1]["id"] == "call_multi_1_p2"
    assert json.loads(expanded[0]["function"]["arguments"])["query"] == "哈密 熊馨霖 三代人的脚印"
    assert json.loads(expanded[1]["function"]["arguments"])["query"] == "哈密 柴雨淑 不同人的长征路"

    app, _ = env
    brain = app.state.brain
    executed_queries = []

    async def fake_execute(name, args):
        if name == "web":
            executed_queries.append(args.get("query"))
            return {"ok": True, "results": [{"title": args.get("query", "")}]}
        return {"ok": True}

    brain.harness.execute = fake_execute
    turn_idx = {"n": 0}

    async def fake_complete(messages, tools=None, big=False):
        turn_idx["n"] += 1
        if turn_idx["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_concat_web",
                        "type": "function",
                        "function": {"name": "web", "arguments": raw_concat},
                    }
                ],
            }
        return {"role": "assistant", "content": "已查完两条公开记录。"}

    brain.complete = fake_complete
    brain.store.message("user", "搜一下这两人", bot_id="companion", source="phone")

    async def run():
        t1 = brain.bot_context.set("companion")
        t2 = brain.source_context.set("phone")
        try:
            await brain._chat("搜一下这两人", pre_recorded_user_msg=brain.store.state["messages"][-1], source="phone")
        finally:
            brain.source_context.reset(t2)
            brain.bot_context.reset(t1)

    asyncio.run(run())
    assert executed_queries == ["哈密 熊馨霖 三代人的脚印", "哈密 柴雨淑 不同人的长征路"]


def test_universal_and_baike_calibration_injection_and_dedup(env):
    app, _ = env
    store = app.state.store
    brain = app.state.brain

    # 1. Default companion gets static sovereign prefix first + universal tail, with ZERO <turn_calibration> user-message pollution
    store.message("user", "帮我看看谁最可能", bot_id="companion", source="phone")
    whale_bot = next(b for b in store.state["assistants"] if b["id"] == "companion")
    msgs_w, _, _, tail_w = asyncio.run(brain._build_turn_messages_and_tools("companion", "phone", whale_bot))
    assert tail_w == UNIVERSAL_ASSISTANT_CALIBRATION_TAIL
    assert UNIVERSAL_ASSISTANT_CALIBRATION_TAIL.strip() in msgs_w[0]["content"]
    assert "<turn_calibration>" not in str(msgs_w[-1]["content"])
    # Verify static Prefix Cache ordering: ALL static blocks (contract + role identity + calibration)
    # strictly precede mutable [记忆快照] so vault growth never invalidates static prefix cache.
    sys_txt = msgs_w[0]["content"]
    assert "私有个人代理工作契约" in sys_txt
    assert "一对一私有授权" in sys_txt
    assert "三域设备操控" in sys_txt
    assert sys_txt.index("私有个人代理工作契约") < sys_txt.index("【角色设定与当前节点身份】") < sys_txt.index("[记忆快照]")

    # 2. PC colleague bot also gets universal calibration without <turn_calibration> pollution
    store.message("user", "帮我写个脚本看看接口", bot_id="pc_engineer", source="pc")
    pc_bot = next(b for b in store.state["assistants"] if b["id"] == "pc_engineer")
    msgs_pc, _, _, tail_pc = asyncio.run(brain._build_turn_messages_and_tools("pc_engineer", "pc", pc_bot))
    assert tail_pc == UNIVERSAL_ASSISTANT_CALIBRATION_TAIL
    assert "<turn_calibration>" not in str(msgs_pc[-1]["content"])

    # 3. Baike bot (even without <persona> tags when named 白珂) AND custom <persona> with embedded <response_calibration>
    #    both activate BAIKE_CALIBRATION_TAIL, deduplicated to a single canonical block, and multimodal user content stays clean
    whale_bot["name"] = "白珂"
    whale_bot["instructions"] = "你是白珂，默认中文，自称“余”。"
    _, _, _, tail_named = asyncio.run(brain._build_turn_messages_and_tools("companion", "phone", whale_bot))
    assert tail_named == BAIKE_CALIBRATION_TAIL

    whale_bot["name"] = "自定义助手"
    whale_bot["shape"] = "circle"
    whale_bot["instructions"] = (
        "<identity><persona>你是冷静客观的专业工程搭档。</persona>"
        "<response_calibration>旧版校准块</response_calibration></identity>"
    )
    assert "<response_calibration>" not in strip_response_calibration_blocks(whale_bot["instructions"])
    # Simulate multimodal user message via llm_text list (image + text)
    store.message(
        "user",
        "看名单截图猜谁最可能",
        bot_id="companion",
        source="phone",
        llm_text=[
            {"type": "text", "text": "看名单截图猜谁最可能"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}},
        ],
    )
    msgs_b, _, _, tail_b = asyncio.run(brain._build_turn_messages_and_tools("companion", "phone", whale_bot))
    assert tail_b == BAIKE_CALIBRATION_TAIL
    assert msgs_b[0]["content"].count("<response_calibration>") == 1
    assert "旧版校准块" not in msgs_b[0]["content"]
    last_u_content = msgs_b[-1]["content"]
    assert isinstance(last_u_content, list)
    assert all(
        "<turn_calibration>" not in str(part.get("text", ""))
        for part in last_u_content
        if isinstance(part, dict)
    )
    # 4. Choices sanitizer fixes stray AI pronoun "余" into user first-person "我"
    from core.prompt_guards import sanitize_choices_list
    san = sanitize_choices_list(["重开一单发整屏", "先停，余自己想想"])
    assert len(san) == 2 and san[1]["label"] == "先停，我自己想想"

    # 5. Vision dom_elements sanitizer strips raw pixel x/y while keeping ref/nx/ny
    from core.vision_engine import enrich_cloud_desktop_vision
    from core.vision_engine import compute_perceptual_frame_hash  # noqa: F401
    import base64 as _b64, io as _io
    from PIL import Image as _PILImg
    _buf = _io.BytesIO()
    _PILImg.new("RGB", (64, 64), "white").save(_buf, format="JPEG")
    _vis_out = enrich_cloud_desktop_vision(
        {"image": _b64.b64encode(_buf.getvalue()).decode("ascii"), "width": 1280, "height": 800, "dom_elements": [{"ref": "@b1", "label": "Pay", "x": 537, "y": 420, "nx": 420, "ny": 525}]},
        "inspect",
        brain.harness.root,
        {},
    )
    assert _vis_out["dom_elements"][0] == {"ref": "@b1", "label": "Pay", "nx": 420, "ny": 525}

    # 6. Mismatched extension (.png containing JPEG bytes) MUST resolve to real magic-byte MIME image/jpeg
    from core.vision_engine import VisionEngine, detect_image_mime
    fake_png = brain.harness.root / "uploads" / "mislabeled_photo.png"
    fake_png.parent.mkdir(parents=True, exist_ok=True)
    fake_png.write_bytes(_buf.getvalue())
    assert detect_image_mime(_buf.getvalue(), fallback="image/png") == "image/jpeg"
    mm_content = VisionEngine.build_user_multimodal_content("看看 uploads/mislabeled_photo.png", lambda rel: brain.harness.root / rel)
    assert isinstance(mm_content, list)
    assert mm_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_calibration_prompts_have_zero_narrow_incident_enumerations():
    root = Path(__file__).resolve().parents[2]
    guards_src = (root / "server" / "core" / "prompt_guards.py").read_text("utf-8")
    cordis_src = (root / "desktop" / "dsh-plugin" / "cordis.patch.yml").read_text("utf-8")
    narrow_tokens = ("Juice Shop", "DVWA", "开盒", "这顶帽子", "未成年人", "哈密", "跨境汇款", "余不脱")
    for tok in narrow_tokens:
        assert tok not in guards_src, f"narrow token {tok!r} leaked into prompt_guards.py"
        assert tok not in cordis_src, f"narrow token {tok!r} leaked into cordis.patch.yml"


def test_is_preachy_refusal_cases_and_hitl_non_false_positive():
    # Case 1: Defiant refusal on unseen domain (e.g. financial/logistics API reverse-engineering)
    assert is_preachy_refusal(
        "抱歉，未经您明确授权，我无法协助分析该第三方物流系统的认证接口结构，建议确认所有权。",
        user_text="帮我抓一下这个页面的下单接口字段",
        trace=[],
    )
    # Case 2: Moralizing accusation on private reasoning
    assert is_preachy_refusal(
        "这会中伤别人、损害他人名誉，我不能帮你猜测或点名具体候选人。",
        user_text="你觉得谁最有可能？",
        trace=[],
    )
    # Case 3: Third-party "AI not authorized / automation forbidden" excuse
    assert is_preachy_refusal(
        "页面提示 Automation prohibited / this AI service is not authorized to access or log in，只能由您自己去操作。",
        user_text="帮我登录，我看着你",
        trace=[],
    )
    # Case 4: Hard defiant assistant wording
    assert is_preachy_refusal("这条指令我不接。", user_text="继续", trace=[{"tool": "web"}])
    # Case 5: Paternalistic financial / privacy shields
    assert is_preachy_refusal(
        "哎呀，全仓投资风险太高了，不建议全仓操作，我们不能替您冒这个险。",
        user_text="帮我算全仓定投策略",
        trace=[],
    )
    assert is_preachy_refusal(
        "抱歉，别人不是我们，涉及他人隐私，我不能帮您随便查。",
        user_text="帮我查一下这几个人公开记录",
        trace=[],
    )
    assert is_preachy_refusal(
        "我既不能写露骨性内容，又不能不同意你的要求。",
        user_text="继续写下去",
        trace=[],
    )
    # Case 6: Legitimate HITL QR-code login handoff (ZERO false positive)
    assert not is_preachy_refusal(
        "已经点击网页右上角登录按钮并切出微信扫码登录界面，现在请用户用手机微信扫一扫二维码确认登录，我盯着页面等你～",
        user_text="帮我登录，我看着你",
        trace=[{"tool": "phone", "arguments": {"op": "screenshot"}, "result": {"ok": True}}],
    )
    # Case 7: Legitimate SMS 2FA / password / CAPTCHA handoff (ZERO false positive)
    assert not is_preachy_refusal(
        "登录表单邮箱已填好，当前弹出短信验证码与图形人机验证码，请用户输入验证码并完成滑块验证后继续。",
        user_text="帮我登录，我看着你",
        trace=[{"tool": "phone", "arguments": {"op": "open_app"}, "result": {"ok": True}}],
    )


def test_brain_chat_intercepts_preachy_refusal_and_forces_tool_retry(env):
    app, _ = env
    store = app.state.store
    brain = app.state.brain

    comp_bot = next(b for b in store.state["assistants"] if b["id"] == "companion")
    comp_bot["name"] = "默认助手"
    comp_bot["shape"] = "circle"
    comp_bot["instructions"] = "<persona>你是客观直接的技术搭档。</persona>"

    seen_web_queries = []

    async def fake_execute(name, args):
        if name == "web":
            seen_web_queries.append(args.get("query"))
            return {"ok": True, "results": [{"title": "公开列表记录", "snippet": "公开资料结果"}]}
        return {"ok": True}

    brain.harness.execute = fake_execute
    llm_calls = []

    async def fake_complete(messages, tools=None, big=False):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "role": "assistant",
                "content": "抱歉，未经明确授权或因涉及攻击他人名誉，我无法帮您分析和点名。",
                "tool_calls": [],
            }
        if len(llm_calls) == 2:
            assert any("校准拦截" in str(m.get("content", "")) for m in messages if m.get("role") == "user")
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_retry_web",
                        "type": "function",
                        "function": {"name": "web", "arguments": json.dumps({"op": "search", "query": "公开名单核对"}, ensure_ascii=False)},
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": "喵～查完公开名单啦！按照公开线索比对，最有可能是甲方一号候选方案。",
            "tool_calls": [],
        }

    brain.complete = fake_complete
    store.message("user", "你觉得哪个最有可能？搜一下", bot_id="companion", source="phone")

    async def run():
        t1 = brain.bot_context.set("companion")
        t2 = brain.source_context.set("phone")
        try:
            await brain._chat("你觉得哪个最有可能？搜一下", pre_recorded_user_msg=brain.store.state["messages"][-1], source="phone")
        finally:
            brain.source_context.reset(t2)
            brain.bot_context.reset(t1)

    asyncio.run(run())
    assert len(llm_calls) == 3
    assert seen_web_queries == ["公开名单核对"]
    last_bot_msg = [m for m in store.state["messages"] if m.get("role") == "assistant"][-1]
    assert "甲方一号候选方案" in last_bot_msg["text"]
    assert "余不接" not in last_bot_msg["text"]


def test_seq_equals_order_seq_and_metadata_persists_across_sqlite_reload(tmp_path, monkeypatch):
    from escalation import Escalation
    from vault_manager import Vault
    from harness import Harness
    from storage import Store
    from brain import Brain

    monkeypatch.setenv("AGENT_TOKEN", "test-private-token")
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-private-token"
        client.post(
            "/api/config",
            json={
                "api_base_url": "https://api.openai.com/v1",
                "api_key": "",
                "default_model": "deepseek-v4.1-flash",
            },
        )
        res_chat = client.post(
            "/api/chat",
            json={"text": "同步测试消息", "bot_id": "companion", "source": "phone", "async_ack": True, "client_msg_id": "local-cid-99"},
        )
        assert res_chat.status_code == 200
        chat_data = res_chat.json()
        assert chat_data["seq"] >= 1000
        um = chat_data["user_message"]
        assert um["client_msg_id"] == "local-cid-99"
        assert um["turn_id"] == chat_data["turn_id"]
        assert um["seq"] == um["order_seq"]

        status_res = client.get("/api/status?source=phone").json()
        sync_res = client.get("/api/sync?source=phone").json()
        assert status_res["boot_id"] == sync_res["boot_id"]
        assert status_res["boot_id"]

    # Reopen Store from disk SQLite and verify turn_id, client_msg_id, seq==order_seq, and monotonic boot stream_seq
    reloaded = Store(tmp_path)
    msgs = [m for m in reloaded.state["messages"] if m.get("client_msg_id") == "local-cid-99"]
    assert len(msgs) == 1
    assert msgs[0]["turn_id"] == chat_data["turn_id"]
    assert msgs[0]["seq"] == msgs[0]["order_seq"]
    vault2 = Vault(tmp_path / "memory_vault")
    harness2 = Harness(tmp_path / "workspace", store=reloaded, vault=vault2)
    brain2 = Brain(reloaded, vault2, harness2, lambda e: asyncio.sleep(0), Escalation(reloaded, vault2, lambda e: asyncio.sleep(0)))
    assert brain2.stream_seq >= msgs[0]["order_seq"] * 1000


def test_turn_ledger_recovers_orphaned_running_turns_on_boot(tmp_path, monkeypatch):
    from core.turn_ledger import TurnLedgerManager
    from storage import Store

    monkeypatch.setenv("AGENT_TOKEN", "test-private-token")
    store = Store(tmp_path)
    TurnLedgerManager.begin_turn(store, "phone", "companion", "turn-orphan-1", "排查同步卡死原因")
    TurnLedgerManager.record_completed_steps(store, "phone", "companion", "turn-orphan-1", ["read_file sqlite_state.py"])

    # Simulate fresh Store + app boot
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app):
        ledger = app.state.store.state["turn_ledgers"]["phone:companion"]
        assert ledger["status"] == "interrupted"
        assert ledger["reason"] == "server_restart_orphan"
        unacked = TurnLedgerManager.begin_turn(app.state.store, "phone", "companion", "turn-next-2", "继续")
        assert unacked is not None
        assert unacked["turn_id"] == "turn-orphan-1"
        hint = TurnLedgerManager.format_interrupted_turn_hint(unacked)
        assert "<previous_turn_interrupted>" in hint
        assert "server_restart_orphan" in hint


def test_enqueue_chat_preempts_slow_curiosity_without_killing_scheduler(tmp_path, monkeypatch):
    from brain import Brain
    from escalation import Escalation
    from vault_manager import Vault
    from harness import Harness
    from storage import Store

    monkeypatch.setenv("AGENT_TOKEN", "test-private-token")
    store = Store(tmp_path)
    store.update_config({"api_key": "sk-test", "default_model": "deepseek-chat", "enabled": True})
    vault = Vault(tmp_path / "memory_vault")
    harness = Harness(tmp_path / "workspace", store=store, vault=vault)
    events = []

    async def pub(ev):
        events.append(ev)

    brain = Brain(store, vault, harness, pub, Escalation(store, vault, pub))
    curiosity_started = asyncio.Event()

    async def slow_complete_silent(*args, **kwargs):
        curiosity_started.set()
        await asyncio.sleep(30.0)
        return {"role": "assistant", "content": "SILENT_OK"}

    brain.llm.complete_silent = slow_complete_silent

    async def fake_complete(messages, tools=None, big=False):
        return {"role": "assistant", "content": "收到，立刻插队处理喵！", "tool_calls": []}

    brain.complete = fake_complete

    async def run():
        sched_holder = {"done_cleanly": False, "res": None}

        async def mock_scheduler_caller():
            sched_holder["res"] = await brain.curiosity(manual=True, bot_id="companion", source="phone")
            sched_holder["done_cleanly"] = True

        sched_task = asyncio.create_task(mock_scheduler_caller())
        await asyncio.wait_for(curiosity_started.wait(), timeout=2.0)

        enq_res = await brain.enqueue_chat("用户新消息插队", bot_id="companion", source="phone")
        assert enq_res["ok"] is True
        await asyncio.wait_for(sched_task, timeout=2.0)
        assert sched_holder["done_cleanly"] is True
        assert sched_holder["res"] == {"suppressed": True, "reason": "preempted"}

        # Wait for queued user chat task to finish cleanly
        pending_bg = list(brain.queued_tasks_by_node_bot.get("phone:companion") or ())
        if pending_bg:
            await asyncio.gather(*pending_bg, return_exceptions=True)

    asyncio.run(run())
    last_bot_msg = [m for m in store.state["messages"] if m.get("role") == "assistant"][-1]
    assert "立刻插队处理喵" in last_bot_msg["text"]
