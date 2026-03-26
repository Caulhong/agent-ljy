"""Context builder for assembling agent prompts."""

from __future__ import annotations

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import detect_image_mime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "TOOLS.md"]  # workspace-level, read-only
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(self, skill_names: Optional[list[str]] = None, user_id: Optional[str] = None) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(user_id=user_id)]

        bootstrap = self._load_bootstrap_files(user_id=user_id)
        if bootstrap:
            parts.append(bootstrap)

        memory = MemoryStore(self.workspace, user_id).get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, user_id: Optional[str] = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        if user_id:
            user_dir = f"{workspace_path}/users/{user_id}"
            user_section = f"""
## Current User Directory (read/write)
Path: {user_dir}
- {user_dir}/USER.md — your profile for this user (name, preferences, instructions)
- {user_dir}/MEMORY.md — long-term memory; write important facts here
- {user_dir}/HISTORY.md — append-only conversation log; each entry starts with [YYYY-MM-DD HH:MM]
- {workspace_path}/sessions.db — session database (do not edit directly)

## Memory Guidelines
- When the user expresses preferences, habits, or explicitly asks you to remember something, immediately write it to {user_dir}/MEMORY.md using write_file — do NOT wait for automatic consolidation.
- Read MEMORY.md before writing to preserve existing entries; append new facts rather than overwriting.
- If a new fact conflicts with an existing entry, remove or update the outdated entry — newer information takes precedence. Never keep two conflicting facts."""
        else:
            user_section = ""

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace Directory (read-only)
Path: {workspace_path}
- {workspace_path}/AGENTS.md, SOUL.md, TOOLS.md — workspace configuration (do NOT modify)
- {workspace_path}/skills/{{skill-name}}/SKILL.md — available skills (do NOT modify)
{user_section}
{platform_policy}
## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- NEVER claim to have done something without a visible tool result confirming it. If asked whether an action was completed, check the actual tool results in conversation history — do NOT infer or assume success from truncated or missing results.
- When referencing past actions, ONLY report what is explicitly present in conversation history. Do NOT fill in gaps or invent outcomes.
- If conversation history contains conflicting information, always prefer the most recent statement — earlier contradicted facts are superseded.
- NEVER reuse a previous tool result as if it is still current. If the current task requires up-to-date information (file contents, command output, search results, etc.), call the tool again — prior results may be stale.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.
- NEVER modify files under the workspace directory — only files under the user directory are writable.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: Optional[str], chat_id: Optional[str]) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self, user_id: Optional[str] = None) -> str:
        """Load workspace-level bootstrap files (read-only) and user-specific USER.md."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        if user_id:
            user_profile = self.workspace / "users" / user_id / "USER.md"
            if user_profile.exists():
                content = user_profile.read_text(encoding="utf-8")
                parts.append(f"## USER.md\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: Optional[list[str]] = None,
        media: Optional[list[str]] = None,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        return [
            {"role": "system", "content": self.build_system_prompt(skill_names, user_id=user_id)},
            *history,
            {"role": "user", "content": merged},
        ]

    def _build_user_content(self, text: str, media: Optional[list[str]]) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: Optional[str],
        tool_calls: Optional[list[dict[str, Any]]] = None,
        reasoning_content: Optional[str] = None,
        thinking_blocks: Optional[list[dict]] = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
