"""消息网关开发冒烟（仅开发用，不引入测试框架）。

跑法（仓库是 venv，不是 .venv）：
  PYTHONPATH=src QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe scripts/smoke_gateway.py

覆盖：
  A 配置存取 + 空凭据 test_connection 不发网络
  B 路由前缀解析
  C 全链路（FakeChannel + FakeDispatcher）：授权发 → 项目 Agent → 回执 / 非授权拒发
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tokbee.core.config import Config
from wokbee.core.settings import WokBeeSettings
from wokbee.core.project_store import ProjectStore
from wokbee.gateway.store import GatewayChannelConfig, GatewayStore
from wokbee.gateway.router import MessageRouter, RouteOutcome
from wokbee.gateway.base import Channel, ChannelMessage, ChannelStatus


def _config(tmp: Path) -> Config:
    """用独立 Config 实例（config_path），不污染、不受共享单例影响。"""
    return Config(str(tmp / "config.json"))


# ── Smoke A：配置 + 空凭据 ─────────────────────────────
def smoke_a() -> None:
    conf = _config(Path(tempfile.mkdtemp()))
    store = GatewayStore(conf)
    cfg = store.get_config()
    assert cfg.enabled is False, "默认应关闭"
    assert cfg.channel == "feishu", "默认频道应为飞书"
    ok, msg = store.test_connection(cfg)  # 空凭据 → 不发网络
    assert ok is False and "未配置" in msg, f"空凭据应直接拒绝：{ok} {msg}"
    cfg.feishu_app_id = "cli_abc"
    cfg.feishu_app_secret = "secret_xyz"
    store.save_config(cfg)
    assert store.get_config().feishu_app_id == "cli_abc", "应能回读保存的 app_id"
    # 微信：空凭据直接拒绝（不发网络），wechat_info() 正确组装/判缺
    cfg.channel = "wechat"
    store.save_config(cfg)
    assert store.get_config().channel == "wechat", "应能回读频道"
    ok, msg = store.test_connection(cfg)  # 空微信凭据 → 不发网络
    assert ok is False and "未配置微信凭据" in msg, f"空微信凭据应直接拒绝：{ok} {msg}"
    cfg.wechat_bot_token = "tok.x"
    cfg.wechat_account_id = "wx_acc"
    cfg.wechat_base_url = "https://ilinkai.weixin.qq.com"
    store.save_config(cfg)
    info = store.get_config().wechat_info()
    assert info.get("botToken") == "tok.x" and info.get("accountId") == "wx_acc", info
    # 默认键首次创建即落盘
    assert conf.get("wokbee.gateway") is not None, "配置键应已写入"
    print("Smoke A OK")


# ── Smoke B：路由前缀 ─────────────────────────────
def smoke_b() -> None:
    p = MessageRouter.parse_route_prefix
    assert p("@项目A 你好") == ("@项目A", "你好"), p("@项目A 你好")
    assert p("#prj_1 hi") == ("#prj_1", "hi"), p("#prj_1 hi")
    assert p("你好") == ("", "你好"), p("你好")
    assert p("@A  x") == ("@A", "x"), p("@A  x")
    assert p("@项目A你好") == ("@项目A你好", ""), p("@项目A你好")
    print("Smoke B OK")


# ── Fake 通道（模拟飞书收/发） ─────────────────────────
class FakeChannel(Channel):
    def __init__(self):
        self._status = ChannelStatus.IDLE
        self._cb = None
        self._sent: list[tuple[ChannelMessage, str]] = []
        self._send_event = threading.Event()

    @property
    def status(self) -> ChannelStatus:
        return self._status

    def on_message(self, callback):
        self._cb = callback

    def start(self):
        self._status = ChannelStatus.CONNECTED

    def stop(self):
        self._status = ChannelStatus.STOPPED

    def send_text(self, msg, text):
        self._sent.append((msg, text))
        self._send_event.set()
        return True, "ok"

    def test_connection(self):
        return True, "ok"

    def trigger(self, msg):
        if self._cb:
            self._cb(msg)

    def wait_send(self, timeout: float = 15.0):
        self._send_event.wait(timeout=timeout)
        if self._sent:
            return self._sent[-1]
        return None

    def clear_sent(self):
        self._sent.clear()
        self._send_event.clear()


class FakeDispatcher:
    def run_chat(self, project, text):
        return SimpleNamespace(ok=True, outcome="success", final_text=f"echo:{text}", error="")

    @staticmethod
    def reply_for(result):
        return (result.final_text or "").strip() or (result.error or result.outcome)


# ── Smoke C：全链路 ─────────────────────────────
def smoke_c() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    proj = pstore.create(title="项目A")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)  # 多频道：只启用飞书
    cfg.channel = "feishu"
    cfg.feishu_default_project_id = proj.id  # 按频道绑定默认项目（issue 3）
    cfg.allow_from = ["ou_allow"]
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "secret_y"
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store,
        settings=settings,
        project_store=pstore,
        provider_store=SimpleNamespace(),
        dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake,
        parent=app,
    )
    mgr.start()
    assert mgr.running, "网关应处于运行态"

    # 授权发送者 → 剥离前缀 → Agent → 回执
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="ou_allow",
        text=f"@{proj.id} 你好", conversation_id="c1", message_id="m1",
    ))
    sent = fake.wait_send(15.0)
    assert sent is not None, "授权发送者未收到回执"
    assert sent[1] == "echo:你好", f"回执应为剥离前缀后的净文本：{sent[1]}"
    # Agent 轨迹应落到项目时间线（由 dispatcher 的 _append_event 写入）
    kinds = [e.kind for e in pstore.list_events(proj.id)]
    assert "user" in kinds, f"缺失 user 事件：{kinds}"

    # 非授权发送者 → 拒绝，且不会跑 Agent
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="ou_evil",
        text=f"@{proj.id} 炸弹词", conversation_id="c2", message_id="m2",
    ))
    denied = fake.wait_send(10.0)
    assert denied is not None, "非授权发送者未收到拒绝回执"
    assert "ou_evil" in denied[1], f"拒绝回复应带上发送者 open_id：{denied[1]}"

    mgr.shutdown(wait=True)
    print("Smoke C OK")


# ── Smoke D：默认放行（允许列表为空 = 对所有人放行，无需先加白） ─────────────
def smoke_d() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    proj = pstore.create(title="项目B")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)  # 多频道：只启用飞书
    cfg.channel = "feishu"
    cfg.feishu_default_project_id = proj.id  # 按频道绑定默认项目（issue 3）
    cfg.allow_from = []  # 空白名单 = 默认放行
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "secret_y"
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake, parent=app,
    )
    mgr.start()

    # 匿名发送者：允许列表为空 → 默认放行，直接跑 Agent（不再被拒、不再写白名单）
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="ou_phonenumber",
        text="你好", conversation_id="c9", message_id="m9",
    ))
    sent = fake.wait_send(15.0)
    assert sent is not None and sent[1] == "echo:你好", "空白名单应放行并跑 Agent"
    # 未自举：允许列表保持为空（个人使用默认放行，不收集任何发送者）
    assert store.get_config().allow_from == [], \
        f"默认放行不应写允许列表：{store.get_config().allow_from}"

    # 显式限定发送者后，未列出的用户应被拒绝
    cfg = store.get_config()
    cfg.allow_from = ["only_me"]
    store.save_config(cfg)
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="ou_stranger",
        text="你好", conversation_id="c10", message_id="m10",
    ))
    denied = fake.wait_send(10.0)
    assert denied is not None and "ou_stranger" in denied[1], "限定后陌生人应被拒绝"

    mgr.shutdown(wait=True)
    print("Smoke D OK")


# ── Smoke WeChat：WeChatChannel 归一化 + send_text 定向（无真实网络/微信） ─────────
def smoke_wechat() -> None:
    from types import SimpleNamespace
    from wokbee.gateway.wechat import WeChatChannel

    cfg = GatewayChannelConfig(channel="wechat")
    ch = WeChatChannel(cfg)
    # 空凭据 → 未配置，不发网络
    ok, msg = ch.test_connection()
    assert ok is False and "未配置微信凭据" in msg, f"{ok} {msg}"

    cfg.wechat_bot_token = "tok"
    cfg.wechat_account_id = "acc"
    cfg.wechat_base_url = "https://x"
    ch = WeChatChannel(cfg)

    # 文本消息 → 归一化
    im = SimpleNamespace(
        from_user="wx_u", text="你好", message_id=123, context_token="ctx1",
        session_id="s1", item_type=1,
        is_image=False, is_file=False, is_video=False, is_voice=False,
    )
    cm = WeChatChannel._to_channel_message(im)
    assert cm.channel == "wechat"
    assert cm.sender_id == "wx_u", cm.sender_id
    assert cm.conversation_id == "wx_u", cm.conversation_id
    assert cm.message_id == "123", cm.message_id
    assert cm.channel_meta["context_token"] == "ctx1"
    assert cm.text == "你好"

    # 图片 → unsupported_type 标记
    im2 = SimpleNamespace(
        from_user="wx_u", text="", message_id=124, context_token="ctx1",
        session_id="s1", item_type=2,
        is_image=True, is_file=False, is_video=False, is_voice=False,
    )
    cm2 = WeChatChannel._to_channel_message(im2)
    assert cm2.channel_meta.get("unsupported_type") == "image", cm2.channel_meta

    # send_text：从 channel_meta 取 context_token 并透传给 client（1800 大块切分）
    sent: dict = {}

    class FakeClient:
        def send_text_chunked(self, to, text, ctx, max_length=0):
            sent.update(to=to, text=text, ctx=ctx, max_length=max_length)

    class FakeBot:
        _ctx_cache: dict = {}
        client = FakeClient()

    ch._bot = FakeBot()
    ch._bot_info = {"botToken": "tok", "accountId": "acc", "baseUrl": "https://x"}
    ok, err = ch.send_text(cm, "回复")
    assert ok is True and err == "ok", (ok, err)
    assert sent["to"] == "wx_u", sent
    assert sent["ctx"] == "ctx1", sent
    assert sent["text"] == "回复", sent
    assert sent["max_length"] == 1800, sent

    # 无 context_token → 明确错误（不 raise）
    cm_noctx = ChannelMessage(
        channel="wechat", sender_id="wx_u", text="hi", conversation_id="wx_u", channel_meta={}
    )
    ok2, err2 = ch.send_text(cm_noctx, "hi")
    assert ok2 is False and "context_token" in err2, (ok2, err2)

    print("Smoke WeChat OK")


# ── Smoke E：Manager 路由一条微信消息（channel_factory 注入假通道，无真实网络） ─────────
def smoke_e_wechat() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    proj = pstore.create(title="微信项目")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("wechat", True)  # 多频道：只启用微信
    cfg.channel = "wechat"
    cfg.wechat_default_project_id = proj.id  # 微信频道绑定自己的默认项目（issue 3）
    cfg.allow_from = ["wx_user"]
    cfg.wechat_bot_token = "tok"
    cfg.wechat_account_id = "acc"
    cfg.wechat_base_url = "https://x"
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake, parent=app,
    )
    mgr.start()
    assert mgr.running, "网关应处于运行态"

    fake.trigger(ChannelMessage(
        channel="wechat", sender_id="wx_user", text=f"@{proj.id} 你好",
        conversation_id="wx_user", message_id="m1", channel_meta={"context_token": "ctx1"},
    ))
    sent = fake.wait_send(15.0)
    assert sent is not None, "微信授权发送者未收到回执"
    assert sent[1] == "echo:你好", f"回执应为剥离前缀后的净文本：{sent[1]}"
    kinds = [e.kind for e in pstore.list_events(proj.id)]
    assert "user" in kinds, f"缺失 user 事件：{kinds}"

    mgr.shutdown(wait=True)
    print("Smoke E OK")


# ── Smoke F：不同频道默认绑定不同项目（issue 3） ─────────────
def smoke_f_channel_default() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    p_f = pstore.create(title="飞书项目")
    p_w = pstore.create(title="微信项目")

    cfg = store.get_config()
    cfg.feishu_default_project_id = p_f.id
    cfg.wechat_default_project_id = p_w.id
    store.save_config(cfg)

    r = MessageRouter(pstore)
    # 无前缀消息按频道路由到各自默认项目
    res = r.route(ChannelMessage(channel="feishu", sender_id="u1", text="你好",
                                 conversation_id="c", message_id="m1"), cfg)
    assert res.outcome == RouteOutcome.OK and res.project_id == p_f.id, res
    res2 = r.route(ChannelMessage(channel="wechat", sender_id="u2", text="你好",
                                  conversation_id="c", message_id="m2"), cfg)
    assert res2.outcome == RouteOutcome.OK and res2.project_id == p_w.id, res2

    # 频道专属为空 → **不再回落全局**（issue：取消绑定即无默认，不能再路由到旧项目）
    cfg2 = store.get_config()
    cfg2.feishu_default_project_id = ""
    cfg2.wechat_default_project_id = ""
    cfg2.default_project_id = p_f.id  # 旧全局默认残留，但不应再被采用
    store.save_config(cfg2)
    res3 = r.route(ChannelMessage(channel="wechat", sender_id="u2", text="hi",
                                  conversation_id="c", message_id="m3"), cfg2)
    assert res3.outcome == RouteOutcome.NO_DEFAULT, res3
    assert res3.reply and "微信项目" in res3.reply, res3
    assert store.get_config().default_project_for("wechat") == "", "取消绑定后应无默认"

    # 迁移：旧配置只有全局默认、无频道专属 → 迁移进各频道专属
    cfg3 = GatewayChannelConfig.from_dict({"default_project_id": p_f.id})
    assert cfg3.default_project_for("feishu") == p_f.id, cfg3.default_project_for("feishu")
    assert cfg3.default_project_for("wechat") == p_f.id, cfg3.default_project_for("wechat")
    print("Smoke F OK")


# ── Smoke G：无默认项目时列出项目清单；@项目 切换并持久化默认（issue 1） ─────────────
def smoke_g_default_switch() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    pA = pstore.create(title="项目A")
    pB = pstore.create(title="项目B")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)  # 多频道：只启用飞书
    cfg.channel = "feishu"
    cfg.allow_from = []  # 默认放行
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "s"
    cfg.feishu_default_project_id = ""
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake, parent=app,
    )
    mgr.start()

    # 1) 无默认 + 无前缀 → 回复列出项目清单，且不跑 Agent
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text="你好",
                                conversation_id="c", message_id="m1"))
    rep = fake.wait_send(10.0)
    assert rep is not None and "未绑定默认项目" in rep[1] and "项目A" in rep[1], f"应列出项目清单：{rep}"
    assert not any(e.kind == "user" for e in pstore.list_events(pA.id)), "未指定项目时不应跑 Agent"

    # 2) @项目ID（按 id，名称不再匹配）→ 命中并设为该频道默认；带内容则切换并运行
    fake.clear_sent()
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text=f"@{pA.id} 你好",
                                conversation_id="c", message_id="m2"))
    rep = fake.wait_send(10.0)
    assert rep is not None and rep[1] == "echo:你好", f"回执错误：{rep}"
    assert store.get_config().feishu_default_project_id == pA.id, store.get_config().feishu_default_project_id
    assert any(e.kind == "user" for e in pstore.list_events(pA.id)), "应把消息落到项目A"

    # 2b) 只发 @项目ID（无内容）→ 纯切换回执确认，**不跑 Agent**（issue：不给「提问内容为空」）
    fake.clear_sent()
    user_before = sum(1 for e in pstore.list_events(pA.id) if e.kind == "user")
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text=f"@{pA.id}",
                                conversation_id="c", message_id="m2b"))
    rep = fake.wait_send(10.0)
    assert rep is not None and "已切换当前频道默认项目" in rep[1], f"应纯切换确认：{rep}"
    user_after = sum(1 for e in pstore.list_events(pA.id) if e.kind == "user")
    assert user_after == user_before, "纯 @项目ID 不应新增 user 事件（不跑 Agent）"

    # 3) 之后无前缀 → 默认落 A
    fake.clear_sent()
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text="继续",
                                conversation_id="c", message_id="m3"))
    rep = fake.wait_send(10.0)
    assert rep is not None and rep[1] == "echo:继续", f"回执错误：{rep}"

    # 4) @项目BID → 切换默认到 B；再发无前缀落 B
    fake.clear_sent()
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text=f"@{pB.id} hi",
                                conversation_id="c", message_id="m4"))
    rep = fake.wait_send(10.0)
    assert rep is not None and rep[1] == "echo:hi", f"回执错误：{rep}"
    assert store.get_config().feishu_default_project_id == pB.id, store.get_config().feishu_default_project_id
    assert any(e.kind == "user" for e in pstore.list_events(pB.id)), "应把消息落到项目B"

    # 5) @项目名（名称）不再解析 → 回「未找到项目」，且不切换不跑
    fake.clear_sent()
    user_before = sum(1 for e in pstore.list_events(pA.id) if e.kind == "user")
    fake.trigger(ChannelMessage(channel="feishu", sender_id="u1", text="@项目A 你好",
                                conversation_id="c", message_id="m5"))
    rep = fake.wait_send(10.0)
    assert rep is not None and "未找到项目" in rep[1], f"名称不应再解析：{rep}"
    assert store.get_config().feishu_default_project_id == pB.id, "名称切换不应改默认"
    user_after = sum(1 for e in pstore.list_events(pA.id) if e.kind == "user")
    assert user_after == user_before, "名称不解析不应跑"

    mgr.shutdown(wait=True)
    print("Smoke G OK")


# ── Smoke H：多频道**同时**在线，关闭一条不影响另一条（BATCH 4 核心） ─────────────
def smoke_h_multi_channel() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    p_f = pstore.create(title="飞书项目")
    p_w = pstore.create(title="微信项目")

    # 每个频道各建一个独立 FakeChannel；feishu 先构建（_CHANNEL_FACTORIES 插入序），按序分配
    fakes: dict[str, FakeChannel] = {}

    def factory(cfg):
        f = FakeChannel()
        fakes["feishu" if len(fakes) == 0 else "wechat"] = f
        return f

    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)
    cfg.set_channel_enabled("wechat", True)  # 两个频道同时启用（不同 IM 不共用网络）
    cfg.feishu_default_project_id = p_f.id
    cfg.wechat_default_project_id = p_w.id
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "s"
    cfg.wechat_bot_token = "tok"
    cfg.wechat_account_id = "acc"
    cfg.wechat_base_url = "https://x"
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=factory, parent=app,
    )
    mgr.start()
    assert set(mgr._channels) == {"feishu", "wechat"}, f"两频道应同时在：{mgr._channels}"
    assert mgr.status_text("feishu") != "未启动", mgr.status_text("feishu")
    assert mgr.status_text("wechat") != "未启动", mgr.status_text("wechat")
    assert mgr.status_text("feishu") == "已连接（等待飞书推送）", mgr.status_text("feishu")
    assert mgr.status_text("wechat") == "已连接（等待微信推送）", mgr.status_text("wechat")

    # 飞书消息路由到飞书默认项目、回执走飞书频道
    fakes["feishu"].trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="你好",
        conversation_id="c_f", message_id="mf",
    ))
    sent = fakes["feishu"].wait_send(10.0)
    assert sent is not None and sent[1] == "echo:你好", sent
    assert any(e.kind == "user" for e in pstore.list_events(p_f.id)), "飞书消息未落盘"
    # 微信消息路由到微信默认项目、回执走微信频道
    fakes["wechat"].trigger(ChannelMessage(
        channel="wechat", sender_id="u2", text="你好",
        conversation_id="c_w", message_id="mw", channel_meta={"context_token": "ctx"},
    ))
    sent = fakes["wechat"].wait_send(10.0)
    assert sent is not None and sent[1] == "echo:你好", sent
    assert any(e.kind == "user" for e in pstore.list_events(p_w.id)), "微信消息未落盘"

    # 关闭微信（sync_channel 按配置停）→ 飞书仍在、微信停
    cfg = store.get_config()
    cfg.set_channel_enabled("wechat", False)
    store.save_config(cfg)
    mgr.sync_channel("wechat")
    assert "wechat" not in mgr._channels, "微信应已停止"
    assert "feishu" in mgr._channels, "飞书不应被微信关闭牵连"
    assert mgr.running and mgr.status_text("feishu") != "未启动", "飞书应继续运行"

    mgr.shutdown(wait=True)
    print("Smoke H OK")


# ── Smoke I：IM 管理指令 @new / @list / @run ─────────────
def smoke_i_commands() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    pA = pstore.create(title="项目A")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)
    cfg.channel = "feishu"
    cfg.allow_from = []  # 默认放行
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "s"
    cfg.feishu_default_project_id = ""
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake, parent=app,
    )
    mgr.start()

    # #list → 列出所有项目（含已有项目A、含项目ID），且不跑 Agent
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="#list",
        conversation_id="c", message_id="mL",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "项目A" in rep[1] and pA.id in rep[1], rep
    assert not any(e.kind == "user" for e in pstore.list_events(pA.id)), "#list 不应跑 Agent"

    # #new 描述 → 新建项目、设目标、绑定为飞书默认
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="#new 帮我做一个家政服务网站",
        conversation_id="c", message_id="mN",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "已创建新项目" in rep[1], rep
    new_id = store.get_config().feishu_default_project_id
    assert new_id, "应绑定新项目为飞书默认"
    proj = pstore.get(new_id)
    assert proj is not None and "家政" in (proj.goal or ""), f"目标应写入：{(proj and proj.goal)!r}"

    # #run → 用默认项目「目标」作为提示词跑 Agent
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="#run",
        conversation_id="c", message_id="mR",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and rep[1] == "echo:帮我做一个家政服务网站", rep
    assert any(e.kind == "user" for e in pstore.list_events(new_id)), "应把目标写进时间线"

    # #help → 返回系统指令说明，且不跑 Agent
    fake.clear_sent()
    user_before = sum(1 for e in pstore.list_events(new_id) if e.kind == "user")
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="#help",
        conversation_id="c", message_id="mH",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "系统指令" in rep[1] and "#new" in rep[1] and "#list" in rep[1], rep
    user_after = sum(1 for e in pstore.list_events(new_id) if e.kind == "user")
    assert user_after == user_before, "#help 不应跑 Agent"

    # 未知指令 #foo → 回未知指令说明，不跑、不改默认
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text="#foo",
        conversation_id="c", message_id="mU",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "未知指令" in rep[1] and "系统指令" in rep[1], rep

    mgr.shutdown(wait=True)
    print("Smoke I OK")


# ── Smoke J：仅 @项目名/#项目id（无正文）→ 只切换默认项目并回执，**不跑 Agent**、不报空内容 ─────────
def smoke_j_switch_only() -> None:
    from PySide6.QtWidgets import QApplication
    from wokbee.gateway.manager import GatewayManager

    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp())
    conf = _config(tmp)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    settings = WokBeeSettings(conf)
    settings.workspace_root = ws
    store = GatewayStore(conf)
    pstore = ProjectStore(settings)
    pA = pstore.create(title="项目A")
    pB = pstore.create(title="项目B")

    fake = FakeChannel()
    cfg = store.get_config()
    cfg.set_channel_enabled("feishu", True)
    cfg.channel = "feishu"
    cfg.allow_from = []  # 默认放行
    cfg.feishu_app_id = "cli_x"
    cfg.feishu_app_secret = "s"
    cfg.feishu_default_project_id = ""
    store.save_config(cfg)

    mgr = GatewayManager(
        store=store, settings=settings, project_store=pstore,
        provider_store=SimpleNamespace(), dispatcher=FakeDispatcher(),
        channel_factory=lambda c: fake, parent=app,
    )
    mgr.start()

    # 1) 仅 @项目AID（无正文）→ 切换默认到 A + 回执确认；**不得**跑 Agent（无 user 事件）、不落空内容
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text=f"@{pA.id}",
        conversation_id="c", message_id="mJ1",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "已切换当前频道默认项目" in rep[1] and "项目A" in rep[1], rep
    assert store.get_config().feishu_default_project_id == pA.id, store.get_config().feishu_default_project_id
    assert not any(e.kind == "user" for e in pstore.list_events(pA.id)), \
        "纯 @项目ID 不应跑 Agent（不该有 user 事件）"

    # 2) 仅 @项目BID → 再切换到 B，仍不跑
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text=f"@{pB.id}",
        conversation_id="c", message_id="mJ2",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and "项目B" in rep[1], rep
    assert store.get_config().feishu_default_project_id == pB.id, store.get_config().feishu_default_project_id
    assert not any(e.kind == "user" for e in pstore.list_events(pB.id)), \
        "纯 @项目BID 不应跑 Agent"

    # 3) @项目AID 你好（**带正文**）→ 仍应切换 + 正常运行（回归：没把带正文的分流失掉）
    fake.clear_sent()
    fake.trigger(ChannelMessage(
        channel="feishu", sender_id="u1", text=f"@{pA.id} 你好",
        conversation_id="c", message_id="mJ3",
    ))
    rep = fake.wait_send(10.0)
    assert rep is not None and rep[1] == "echo:你好", rep
    assert store.get_config().feishu_default_project_id == pA.id, store.get_config().feishu_default_project_id
    assert any(e.kind == "user" for e in pstore.list_events(pA.id)), "带正文的 @项目ID 应跑 Agent"

    mgr.shutdown(wait=True)
    print("Smoke J OK")


def main() -> int:
    smoke_a()
    smoke_b()
    smoke_c()
    smoke_d()
    smoke_wechat()
    smoke_e_wechat()
    smoke_f_channel_default()
    smoke_g_default_switch()
    smoke_h_multi_channel()
    smoke_i_commands()
    smoke_j_switch_only()
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
