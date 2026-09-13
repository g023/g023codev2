"""Child agents, task board, and team roster. No HTTP at import."""

from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from g023v2.constants import (
    DEFAULT_CHILD_EFFORT,
    HEARTBEAT_STALE_SECONDS,
    MAX_TEAM_MEMBERS,
    MAX_TEAM_TASKS,
)
from g023v2.messages import AgentMessage, MessageQueue
from g023v2.orchestration import extract_metadata, run_turn, strip_harness_meta_in_span
from g023v2.plan import compact_delegate_result, normalize_role
from g023v2.schemas import dispatch_allowlist
from g023v2.tools import ProjectTools
from g023v2.util import clamp_effort, log

if TYPE_CHECKING:
    from g023v2.session import Session


class Agent:
    """A child in parent-child mode, or a persistent member in team mode."""

    def __init__(
        self,
        name: str,
        session: Session,
        effort: int = DEFAULT_CHILD_EFFORT,
        workspace: Path | None = None,
        persistent: bool = False,
        role: str = "",
    ):
        self.name = name
        self.session = session
        self.effort = clamp_effort(effort)
        self.workspace = workspace or session.root
        self.persistent = persistent
        self.role = normalize_role(role) if not persistent else (role or "")
        self.depth = 0
        self.spawned_by: str | None = None
        self.result_consumed = False
        self.messages = MessageQueue(name)
        self.inbox_tasks: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.history: list[dict[str, Any]] = []
        self.shutdown = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "idle"
        self.result = ""
        self.error = ""
        self.summary = ""
        self.current_task_id: str | None = None
        self.heartbeat = time.monotonic()
        self.completion_event = threading.Event()
        self.lock = threading.Lock()
        self.tools = ProjectTools(self.workspace, session.memory)

    def start_one_shot(self, task: str, context: str = "", fresh: bool = True, parent_history: list | None = None) -> None:
        self.persistent = False
        self.thread = threading.Thread(
            target=self._one_shot_entry,
            args=(task, context, fresh, parent_history),
            name=f"agent-{self.name}",
            daemon=True,
        )
        self.thread.start()

    def start_persistent(self) -> None:
        self.persistent = True
        self.thread = threading.Thread(
            target=self._persistent_loop,
            name=f"agent-{self.name}",
            daemon=True,
        )
        self.thread.start()

    def _one_shot_entry(self, task: str, context: str, fresh: bool, parent_history: list | None) -> None:
        if not fresh and parent_history:
            self.history = list(parent_history)
        try:
            self._run_task(task, context, task_id=None)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log(f"[agent {self.name}] one-shot failed: {self.error}\n{traceback.format_exc()}")
        finally:
            with self.lock:
                self.state = "completed" if not self.error else "failed"
            self.completion_event.set()

    def _persistent_loop(self) -> None:
        with self.lock:
            self.state = "idle"
        while not self.shutdown.is_set():
            self.heartbeat = time.monotonic()
            try:
                item = self.inbox_tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            task_id = item.get("task_id")
            task_text = item.get("task", "")
            context = item.get("context", "")
            self.cancel_event.clear()
            self.error = ""
            self.result = ""
            try:
                self._run_task(task_text, context, task_id=task_id)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log(f"[agent {self.name}] task {task_id} failed: {self.error}\n{traceback.format_exc()}")
            finally:
                self.current_task_id = None
                with self.lock:
                    self.state = "idle"
        with self.lock:
            self.state = "stopped"

    def _run_task(self, task: str, context: str, task_id: str | None) -> None:
        with self.lock:
            self.state = "running"
        self.current_task_id = task_id
        self.cancel_event.clear()
        self.heartbeat = time.monotonic()

        pending = self.messages.drain()
        pending_lines = [f"- [{m.sender}] {m.content}" for m in pending]

        parts = [f"TASK: {task}"]
        role = normalize_role(self.role) if not self.persistent else (self.role or "")
        if role:
            parts.append(f"ROLE: {role}")
        if context:
            parts.append(f"CONTEXT: {context}")
        if pending_lines:
            parts.append("PENDING MESSAGES:\n" + "\n".join(pending_lines))
        self.history.append({"role": "user", "content": [{"type": "input_text", "text": "\n\n".join(parts)}]})

        mark = len(self.history)
        is_lead = (not self.persistent) and role == "lead" and self.depth == 0
        log(
            f"[agent {self.name}] starting turn "
            f"(effort={self.effort} role={role or '-'} depth={self.depth} lead={is_lead})"
        )
        result = run_turn(
            self.history,
            self.effort,
            self.tools,
            self.session.memory,
            tool_schemas=self.session.tool_schemas,
            system_prompt=self.session.system_prompt,
            session=self.session,
            cancel_event=self.cancel_event,
            message_source=self.messages,
            log_prefix=self.name,
            on_pulse=self.pulse,
            allowed_names=dispatch_allowlist(
                self.session.teams_enabled,
                goal_mode=False,
                delegate=True,
                lead=is_lead,
            ),
            delegate_mode=True,
        )

        meta, cleaned = extract_metadata(result.text)
        strip_harness_meta_in_span(self.history, mark)
        self.effort = clamp_effort(result.effort)
        self.summary = meta.get("summary", "")
        self.result = compact_delegate_result(cleaned)
        self.result_consumed = False
        if result.cancelled:
            self.result = (cleaned + "\n[cancelled]").strip()

        if task_id and self.session.team:
            try:
                if result.cancelled:
                    self.session.team.task_board.release(task_id, self.name)
                else:
                    self.session.team.task_board.complete(task_id, self.name, self.result or "(empty result)")
            except Exception as e:
                log(f"[agent {self.name}] task finalization failed for {task_id}: {e}")

    def send(self, content: str, kind: str = "supplement", priority: int = 0, sender: str = "parent") -> str:
        msg = AgentMessage(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            recipient=self.name,
            content=content,
            kind=kind,
            priority=priority,
        )
        return self.messages.enqueue(msg)

    def interject(self, content: str, sender: str = "parent") -> str:
        return self.send(content, kind="interject", priority=10, sender=sender)

    def pulse(self) -> None:
        self.heartbeat = time.monotonic()

    def cancel(self) -> None:
        self.cancel_event.set()

    def stop(self) -> None:
        self.shutdown.set()
        self.cancel_event.set()
        if self.persistent:
            try:
                self.inbox_tasks.put_nowait(None)
            except queue.Full:
                pass

    @property
    def thread_live(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def heartbeat_stale(self) -> bool:
        return (time.monotonic() - self.heartbeat) >= HEARTBEAT_STALE_SECONDS

    @property
    def alive(self) -> bool:
        """True while the worker thread is running. Heartbeat is a separate status."""
        return self.thread_live

    def status(self) -> str:
        with self.lock:
            role_bit = f" role={self.role!r}" if self.role else ""
            if self.depth:
                role_bit += f" depth={self.depth}"
            if self.thread is None:
                return f"not started{role_bit}"
            if self.thread.is_alive() and self.heartbeat_stale:
                base = "running" if self.state == "running" else (self.state or "idle")
                return f"{base} (heartbeat stale){role_bit}"
            if not self.thread.is_alive() and self.state != "completed":
                if self.error:
                    return f"failed ({self.error[:60]}){role_bit}"
                return f"exited{role_bit}"
            if self.error:
                return f"failed ({self.error[:60]}){role_bit}"
            if self.state == "running":
                return f"running{role_bit}"
            if self.persistent:
                return f"idle{role_bit}"
            return f"{self.state}{role_bit}"


class TaskBoard:
    def __init__(self, max_tasks: int = MAX_TEAM_TASKS):
        self.max_tasks = max_tasks
        self.tasks: dict[str, dict[str, Any]] = {}
        self.revision = 0
        self.lock = threading.Lock()

    def create(self, title: str, details: str, depends_on: list[str] | None = None, files: list[str] | None = None) -> str:
        with self.lock:
            active = sum(
                1
                for t in self.tasks.values()
                if not t.get("deleted") and t.get("status") != "completed"
            )
            if active >= self.max_tasks:
                raise RuntimeError(f"task board is full ({self.max_tasks} active tasks)")
            task_id = f"task-{len(self.tasks) + 1}"
            self.tasks[task_id] = {
                "id": task_id,
                "title": title,
                "details": details,
                "depends_on": depends_on or [],
                "files": files or [],
                "status": "pending",
                "owner": None,
                "result": None,
                "deleted": False,
            }
            self.revision += 1
            return task_id

    def claim(self, task_id: str, member: str) -> dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task.get("deleted"):
                raise RuntimeError(f"unknown task: {task_id}")
            if task["status"] != "pending":
                raise RuntimeError(f"task {task_id} is {task['status']}, not pending")
            for dep in task["depends_on"]:
                dep_task = self.tasks.get(dep)
                if not dep_task or dep_task["status"] != "completed":
                    raise RuntimeError(f"task {task_id} depends on {dep} which is not complete")
            task["status"] = "running"
            task["owner"] = member
            self.revision += 1
            return dict(task)

    def release(self, task_id: str, member: str) -> dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task.get("deleted"):
                raise RuntimeError(f"unknown task: {task_id}")
            if task["owner"] != member:
                raise RuntimeError(f"task {task_id} owned by {task['owner']}, not {member}")
            task["status"] = "pending"
            task["owner"] = None
            self.revision += 1
            return dict(task)

    def complete(self, task_id: str, member: str, result: str) -> dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task.get("deleted"):
                raise RuntimeError(f"unknown task: {task_id}")
            if task["owner"] != member:
                raise RuntimeError(f"task {task_id} owned by {task['owner']}, not {member}")
            task["status"] = "completed"
            task["result"] = result
            self.revision += 1
            return dict(task)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(t) for t in self.tasks.values() if not t.get("deleted")]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            t = self.tasks.get(task_id)
            return dict(t) if t else None


class AgentTeam:
    def __init__(self, captain: Session, max_members: int = MAX_TEAM_MEMBERS):
        self.captain = captain
        self.max_members = max_members
        self.members: dict[str, Agent] = {}
        self.task_board = TaskBoard()
        self.captain_inbox = MessageQueue("captain")
        self.lock = threading.Lock()

    def create_member(self, name: str, role: str, effort: int = DEFAULT_CHILD_EFFORT) -> str:
        with self.lock:
            if name in self.members:
                raise RuntimeError(f"member {name} already exists")
            if len(self.members) >= self.max_members:
                raise RuntimeError(f"team is full ({self.max_members} members)")
            member = Agent(
                name=name,
                session=self.captain,
                effort=effort,
                workspace=self.captain.root,
                persistent=True,
                role=role,
            )
            self.members[name] = member
            member.start_persistent()
            return f"created persistent member {name} role='{role}' effort={clamp_effort(effort)}"

    def dispatch_task(self, task_id: str, member: str) -> None:
        if member not in self.members:
            raise RuntimeError(f"unknown member: {member}")
        m = self.members[member]
        if not m.thread_live:
            raise RuntimeError(f"member {member} is not alive (status: {m.status()})")
        task = self.task_board.get(task_id)
        if not task:
            raise RuntimeError(f"unknown task: {task_id}")
        text = f"{task['title']}\n\n{task['details']}"
        if task.get("files"):
            text += "\n\nFiles in scope:\n" + "\n".join(f"- {f}" for f in task["files"])
        m.inbox_tasks.put({"task_id": task_id, "task": text})

    def send_message(self, sender: str, recipient: str, content: str, kind: str = "message") -> str:
        if recipient == "captain":
            msg = AgentMessage(
                id=uuid.uuid4().hex[:12],
                sender=sender,
                recipient="captain",
                content=content,
                kind=kind,
            )
            self.captain_inbox.enqueue(msg)
            return msg.id
        if recipient not in self.members:
            raise RuntimeError(f"unknown recipient: {recipient}")
        msg = AgentMessage(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            recipient=recipient,
            content=content,
            kind=kind,
        )
        self.members[recipient].messages.enqueue(msg)
        return msg.id

    def status(self) -> str:
        lines = ["ROSTER:"]
        for name, member in self.members.items():
            pending = len(member.messages.pending())
            alive = "alive" if member.thread_live else "DEAD"
            lines.append(
                f"  {name}: {member.status()} [{alive}] effort={member.effort} pending_msgs={pending}"
            )
        lines.append("TASKS:")
        for t in self.task_board.list_tasks():
            owner = t["owner"] or "-"
            deps = ",".join(t["depends_on"]) or "-"
            lines.append(f"  {t['id']}: [{t['status']}] owner={owner} deps={deps}  {t['title']}")
        pending = self.captain_inbox.pending()
        if pending:
            lines.append(f"CAPTAIN INBOX: {len(pending)} pending")
        return "\n".join(lines)

    def drain_captain_inbox(self) -> str:
        msgs = self.captain_inbox.drain()
        if not msgs:
            return ""
        return "\n".join(f"[{m.sender}] {m.content}" for m in msgs)
