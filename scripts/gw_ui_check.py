"""开发用：离屏验证 GatewayWorkspace 的二维码流（渲染/倒计时/超时/回填）。

⚠️ 必须先建 QApplication 再调 `_render_qr`（它内部建 QPixmap/QPainter，需要 GUI 后端）。
"""
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from tokbee.core.config import Config
from tokbee.ui.styles.theme import Theme
from wokbee.core.settings import WokBeeSettings
from wokbee.core.project_store import ProjectStore
from wokbee.gateway.manager import GatewayManager
from wokbee.gateway.store import GatewayStore
from wokbee.ui.gateway_workspace import _render_qr, fmt_duration, GatewayWorkspace

# `tip()` 是模态框（阻塞），离屏无用户可点。屏蔽掉以让状态机跑完。
import wokbee.ui.gateway_workspace as _gw
_gw._tip = lambda *a, **k: None


pm = _render_qr('https://secure.feishu.cn/connect/app/verify?token=abc123', 260)
assert pm is not None and not pm.isNull() and pm.width() > 0, 'QR pixmap invalid'
print('QR pixmap:', pm.width(), 'x', pm.height())

assert fmt_duration(59) == '00:59'
assert fmt_duration(3661) == '1:01:01'
assert fmt_duration(0) == '00:00'
print('fmt_duration OK')

tmp = Path(tempfile.mkdtemp()); conf = Config(str(tmp / 'config.json'))
ws = tmp / 'ws'; ws.mkdir(parents=True, exist_ok=True)
(ws / 'prj_1').mkdir()
(ws / 'prj_1' / 'project.json').write_text('{"title":"项目A","id":"prj_1"}', encoding='utf-8')
st = WokBeeSettings(conf); st.workspace_root = ws
mgr = GatewayManager(store=GatewayStore(conf), settings=st, project_store=ProjectStore(st), parent=None)
from wokbee.gateway.base import Channel, ChannelStatus

class FakeCh(Channel):
    @property
    def status(self):
        return ChannelStatus.CONNECTED
    def on_message(self, cb):
        pass
    def start(self):
        pass
    def stop(self):
        pass
    def send_text(self, msg, text):
        return True, "ok"
    def test_connection(self):
        return True, "ok"

mgr._channel_factory = lambda cfg: FakeCh()  # 避免真实网络
w = GatewayWorkspace(Theme(), mgr)
pan = w._panels["feishu"]

# 不复用 _on_scan：它会起真实网络 worker（阻塞）。直接驱动状态机。
pan._set_qr_state_connecting()
pan._states.setCurrentWidget(pan._qr_page)
assert pan._states.currentWidget() is pan._qr_page, 'should be on qr page'
assert '正在连接飞书' in pan._qr_status.text(), pan._qr_status.text()
print('connecting state OK:', pan._qr_status.text())

pan._on_qr_ready('https://secure.feishu.cn/connect/app/verify?token=xyz', 3600)
assert pan._qr_label.pixmap() is not None and pan._qr_label.pixmap().width() > 0, 'QR label has no pixmap'
assert pan._countdown_timer.isActive(), 'countdown should be running'
assert ('正在添加新机器人' in pan._qr_status.text()) or ('二维码有效' in pan._qr_status.text()), pan._qr_status.text()
assert '1.' in pan._qr_steps.text() and '2.' in pan._qr_steps.text() and '3.' in pan._qr_steps.text()
print('qr_ready transition OK:', pan._qr_status.text())
print('steps:', repr(pan._qr_steps.text()))

pan._on_provision_done('cli_abc', 'secret_zzz')
assert pan._states.currentWidget() is pan._connected_page, 'should be on connected page'
assert 'cli_abc' in pan._conn_app.text(), pan._conn_app.text()
assert pan._enabled.isChecked(), 'provision 后应自动启用'
assert mgr.running, 'provision 后应自动启动长连接'
print('connected transition OK, conn_app=', pan._conn_app.text(), 'status=', pan._conn_status.text())

# 已有凭据再进入 → refresh 应停在「已接入」页，不再要求扫码 (#3)
pan.refresh()
assert pan._states.currentWidget() is pan._connected_page, '已有凭据应显示已接入页'
print('refresh keeps connected page OK:', pan._conn_status.text())

pan._states.setCurrentWidget(pan._idle_page)
wd = QTimer(); wd.setSingleShot(True)
wd.timeout.connect(lambda: pan._qr_status.setText('连接飞书超时（>30s），请检查网络后重试。'))
wd.timeout.emit()
assert '超时' in pan._qr_status.text(), pan._qr_status.text()
print('watchdog OK:', pan._qr_status.text())


# ── 微信面板：频道切换 + 扫码状态机（无真实网络/登录） ──────────────────
wpan = w._panels["wechat"]
w._rail.select("wechat")
assert w._stack.currentWidget() is wpan, '应切到微信面板'
print('channel switch to wechat OK')

# 空凭据 → idle 页
wpan.refresh()
assert wpan._states.currentWidget() is wpan._idle_page, '空凭据应显示 idle 页'

wpan._set_qr_state_connecting()
wpan._states.setCurrentWidget(wpan._qr_page)
assert '正在连接微信服务' in wpan._qr_status.text(), wpan._qr_status.text()
print('wechat connecting OK:', wpan._qr_status.text())

wpan._on_qr_ready('https://ilinkai.weixin.qq.com/qr?token=xyz')
assert wpan._qr_label.pixmap() is not None and wpan._qr_label.pixmap().width() > 0, 'wechat QR pixmap missing'
assert '扫一扫' in wpan._qr_status.text(), wpan._qr_status.text()
print('wechat qr_ready OK:', wpan._qr_status.text())

# 扫码确认 → 写 store + 已接入页 + 自动启用
wpan._on_provision_done({
    "botToken": "tok_x", "accountId": "wx_acc", "baseUrl": "https://ilinkai.weixin.qq.com", "userId": "",
})
assert wpan._states.currentWidget() is wpan._connected_page, '应切到已接入页'
assert wpan._enabled.isChecked(), '扫码后应自动启用'
cfg = wpan.store.get_config()
assert cfg.channel == "wechat", cfg.channel
assert cfg.wechat_bot_token == "tok_x", cfg.wechat_bot_token
assert 'wx_acc' in wpan._conn_app.text(), wpan._conn_app.text()
print('wechat provision done OK:', wpan._conn_app.text())

# 已有凭据 refresh → 停在已接入页
wpan.refresh()
assert wpan._states.currentWidget() is wpan._connected_page, '已有微信凭据应显示已接入页'
# 刷新后默认频道仍应选中微信；面板与频道栏一致（issue 1 回归）
assert w._stack.currentWidget() is wpan, 'refresh 后应仍显示微信面板'
assert w._rail._buttons["wechat"].isChecked(), '选中微信时频道栏应勾选微信'
assert not w._rail._buttons["feishu"].isChecked(), '选中微信时频道栏不应勾选飞书'
print('wechat refresh keeps connected OK')

print('ALL UI CHECKS PASS')
