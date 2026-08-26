"""定时任务执行器：按类型分派，支持无人值守跑 WokBee 项目。"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Any

from tokbee.core.ai_client import AIClient
from tokbee.core.provider_store import ProviderStore, ResolvedModel

from wokbee.core.models import ApprovalFlags, Project, ProjectEvent, ProjectStatus
from wokbee.core.project_store import ProjectStore
from wokbee.engine.runner import AgentRunner, RunRequest, resolve_model_for_project
from wokbee.core.settings import WokBeeSettings

from autobee.core.models import ScheduledTask, TaskType
from autobee.core.store import AutoBeeStore
from autobee.engine.wecom import push_wecom

logger = logging.getLogger("autobee")


class TaskExecutor:
    """按任务类型执行一次定时任务。

    APScheduler 在线程池中调用 run()，自身是阻塞同步的。回调只操作
    AutoBeeStore / ProjectStore（已有锁保护），不触碰任何 Qt 控件。
    """

    def __init__(
        self,
        store: AutoBeeStore | None = None,
        project_store: ProjectStore | None = None,
        provider_store: ProviderStore | None = None,
        settings: WokBeeSettings | None = None,
    ):
        self.store = store or AutoBeeStore()
        self.project_store = project_store or ProjectStore()
        self.provider_store = provider_store or ProviderStore()
        self.settings = settings or WokBeeSettings()
        # 按 project_id 加锁，防两个任务并发跑同一项目、共用 checkpointer 互相覆盖
        self._project_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_id] = lock
            return lock

    def run(self, task: ScheduledTask) -> dict:
        """执行任务，返回 {ok, message, error}；可选结果推送企业微信。"""
        ttype = task.task_type
        if ttype == TaskType.TEXT:
            result = self._run_text(task)
        elif ttype == TaskType.SCRIPT:
            result = self._run_script(task)
        elif ttype == TaskType.WOKBEE:
            result = self._run_wokbee(task)
        else:
            return {"ok": False, "message": "", "error": f"未知任务类型：{ttype}"}
        self._maybe_push(task, result)
        return result

    def _maybe_push(self, task: ScheduledTask, result: dict) -> None:
        """任务结果（成功或失败）按需推送到企业微信。"""
        if not task.push_wecom:
            return
        webhook = (task.webhook_url or "").strip()
        payload = (result.get("message") or "").strip() or (result.get("error") or "").strip()
        if not webhook:
            suffix = "[推送失败] 未配置企业微信 Webhook"
            result["message"] = " ".join(x for x in (result.get("message"), suffix) if x)
            return
        if not payload:
            payload = "(任务无输出)"
        ok, msg = push_wecom(webhook, payload, msgtype=task.msgtype, mention=task.mention)
        if not ok:
            base = (result.get("error") or result.get("message") or "").strip("；")
            result["error"] = f"{base}；推送失败：{msg}".strip("；") if base else f"推送失败：{msg}"

    # ── text ───────────────────────────────────────────────
    def _run_text(self, task: ScheduledTask) -> dict:
        if not task.use_ai:
            return {"ok": True, "message": task.content or "（无内容）", "error": ""}
        try:
            model = self._resolve_model(task, for_gen=False)
            client = AIClient(model.api_host, model.api_key, model.model_id, family=model.family)
            prompt = task.content or task.description or "请生成一段文本。"
            resp = client.chat(
                [
                    {"role": "system", "content": "你是文本生成助手，根据要求输出正文。不要解释，只输出正文。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=1000,
            )
            content = (resp.content or "").strip() or (resp.reasoning_content or "").strip()
            if not content:
                return {"ok": False, "message": "", "error": "AI 未返回正文"}
            return {"ok": True, "message": content, "error": ""}
        except Exception as e:
            return {"ok": False, "message": "", "error": str(e)}

    # ── script ─────────────────────────────────────────────
    def _run_script(self, task: ScheduledTask) -> dict:
        code = (task.code or "").strip()
        if not code:
            return {"ok": False, "message": "", "error": "脚本代码为空"}
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=task.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "", "error": f"脚本执行超时（> {task.timeout_s} 秒）"}
        except Exception as e:
            return {"ok": False, "message": "", "error": f"脚本执行失败：{e}"}
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            return {"ok": True, "message": out or "（执行成功，无输出）", "error": err}
        return {
            "ok": False,
            "message": out or "脚本执行失败",
            "error": err or f"退出码 {proc.returncode}",
        }

    # ── wokbee ─────────────────────────────────────────────
    def _run_wokbee(self, task: ScheduledTask) -> dict:
        pid = (task.project_id or "").strip()
        if not pid:
            return {"ok": False, "message": "", "error": "未关联 WokBee 项目"}
        project = self.project_store.get(pid)
        if project is None:
            return {"ok": False, "message": "", "error": f"WokBee 项目不存在：{pid}"}
        with self._project_lock(pid):
            try:
                resolved = self._resolve_exec_model(task, project)
                approval = ApprovalFlags(
                    skip_read=True, skip_write=True,
                    skip_routine=True, skip_high_risk=True,
                )
                user_message = (task.user_message or "").strip() or project.goal or ""
                req = RunRequest(
                    project=project,
                    project_root=self.project_store.path_for(project.id),
                    user_message=user_message,
                    resolved=resolved,
                    approval=approval,
                    max_steps=task.max_steps,
                )
                runner = AgentRunner(self.settings, self.provider_store)
                self._wire_runner_callbacks(runner, task, project)
                self.project_store.set_status(
                    project.id, ProjectStatus.RUNNING, current_step="AutoBee 定时触发",
                )
                result = runner.run(req)
            except Exception as e:
                logger.exception("定时运行 WokBee 项目失败: %s", pid)
                self.project_store.set_status(
                    project.id, ProjectStatus.FAILED, current_step="自动失败",
                )
                return {"ok": False, "message": "", "error": str(e)}

        self._finalize_wokbee_status(project.id, result)
        outcome = getattr(result, "outcome", "failed")
        if getattr(result, "ok", False):
            message = (result.final_text or "").strip() or result.outcome
            return {"ok": True, "message": message[:4000], "error": ""}
        return {
            "ok": False,
            "message": (result.final_text or "").strip() or result.outcome,
            "error": (result.error or "")[:4000],
        }

    def _resolve_exec_model(self, task: ScheduledTask, project: Project) -> ResolvedModel:
        """优先任务约束的 exec 模型，否则回落到项目自身解析链。"""
        if task.exec_provider and task.exec_model_id:
            resolved = self.provider_store.resolve(task.exec_provider, task.exec_model_id)
            if resolved:
                return resolved
        return resolve_model_for_project(project, self.settings, self.provider_store)

    def _wire_runner_callbacks(self, runner: AgentRunner, task: ScheduledTask, project: Project) -> None:
        def _on_event(kind: str, content: str, meta: dict | None):
            m = dict(meta or {})
            m["autobee_task_id"] = task.id
            try:
                self.project_store.append_event(
                    project.id, ProjectEvent(kind=kind, content=content, meta=m),
                )
            except Exception:
                logger.exception("写入项目事件失败")

        def _on_approval(pending: list):
            # 无人值守：自动放行
            try:
                runner.resolve_approval([{"type": "approve"}] * len(pending))
            except Exception:
                logger.exception("自动审批失败")

        def _on_ask_user(payload: dict):
            # 无人值守：自动选择第一项，避免无限等待
            questions = payload.get("questions") or []
            answers = [
                {"id": str(q.get("id") or ""), "selected": [str(q["options"][0])]}
                for q in questions if isinstance(q, dict) and q.get("options")
            ]
            try:
                runner.resolve_ask_user({"answers": answers})
            except Exception:
                logger.exception("自动回答澄清失败")

        runner.on_event = _on_event
        runner.on_approval_needed = _on_approval
        runner.on_ask_user_needed = _on_ask_user

    def _finalize_wokbee_status(self, project_id: str, result: Any) -> None:
        outcome = getattr(result, "outcome", "failed")
        if outcome == "success":
            self.project_store.set_status(
                project_id, ProjectStatus.DONE, current_step="完成",
                progress_done=1, progress_total=1,
            )
        elif outcome == "cancelled":
            self.project_store.set_status(project_id, ProjectStatus.IDLE, current_step="已取消")
        else:
            self.project_store.set_status(project_id, ProjectStatus.FAILED, current_step="自动失败")

    def _resolve_model(self, task: ScheduledTask, for_gen: bool) -> ResolvedModel:
        """解析生成/执行的模型：任务绑定 → 厂商默认 → 列表第一个。"""
        if for_gen:
            pid, mid = task.gen_provider, task.gen_model_id
        else:
            pid, mid = task.exec_provider, task.exec_model_id
        if pid and mid:
            resolved = self.provider_store.resolve(pid, mid)
            if resolved:
                return resolved
        default = self.provider_store.resolve_default()
        if default:
            return default
        first = self.provider_store.first_resolved()
        if not first:
            raise ValueError("没有可用模型，请先在「AI配置 → 厂商设置」中启用模型并填写 Key/Host。")
        return first
