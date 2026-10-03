#!/usr/bin/env python3
"""Quota-aware, safe local dispatcher for official coding-agent CLIs."""
from __future__ import annotations

import argparse
import json
import re
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLE_ORDER = {
    "thinker": ("claude", "codex", "antigravity"),
    "coder": ("codex", "claude", "antigravity"),
    "reviewer": ("antigravity", "claude", "codex"),
}


@dataclass
class Attempt:
    provider: str
    classification: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    detail: str = ""
    tokens: dict | None = None
    raw: str = ""  # salida cruda del CLI (stream), para pasar el estado a quien continua


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def classify(returncode: int, stdout: str, stderr: str, patterns: dict[str, list[str]]) -> str:
    """Classify transport failure before treating non-empty stdout as an answer."""
    combined = (stdout + "\n" + stderr).casefold()
    for kind in ("quota", "temporary", "auth"):
        if any(pattern.casefold() in combined for pattern in patterns.get(kind, [])):
            return kind
    if not stdout.strip():
        return "no_valid_response"
    # A non-empty answer is authoritative even if it reports failed tests or a
    # rejected review. Do not mistake a technical conclusion for provider failure.
    return "valid"


def parse_ndjson(stdout: str) -> list[dict]:
    """Parse a newline-delimited JSON stream, tolerating trailing partial lines.

    A CLI that dies before its protocol starts leaves non-JSON text. That is not
    a stream with noise in it, so an all-malformed body raises and is classified
    from stderr instead of being read as an empty answer.
    """
    events: list[dict] = []
    malformed = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events and malformed:
        raise ValueError("no parseable NDJSON events")
    return events


CLAUDE_FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def claude_written_files(events: list[dict]) -> list[str]:
    """Files edited by claude, read from its tool_use blocks."""
    written: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in CLAUDE_FILE_TOOLS:
                continue
            params = block.get("input")
            target = params.get("file_path") if isinstance(params, dict) else None
            written.append(target if isinstance(target, str) and target else block["name"])
    return written


def codex_written_files(events: list[dict]) -> list[str]:
    """Files changed by codex, read from its file_change items."""
    written: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "file_change":
            continue
        changes = item.get("changes") or item.get("files")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict):
                    path = change.get("path") or change.get("file")
                    written.append(path if isinstance(path, str) else "file_change")
                elif isinstance(change, str):
                    written.append(change)
        else:
            written.append(item.get("path") or "file_change")
    return written


def work_done_answer(written: list[str]) -> str:
    listing = "\n".join(f"- {path}" for path in dict.fromkeys(written))
    return f"Archivos modificados en el worktree:\n{listing}"


def decode_codex(events: list[dict]) -> tuple[str, bool, list[str]]:
    answer = "\n".join(
        event.get("item", {}).get("text", "")
        for event in events
        if event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "agent_message"
    )
    errors = [
        json.dumps(event.get("error", event.get("message", "")))
        for event in events
        if event.get("type") in ("error", "turn.failed")
    ]
    if not answer.strip() and not errors:
        # Same rule as the other providers: edited files are the work, and a
        # missing closing message must not discard the worktree.
        written = codex_written_files(events)
        if written:
            return work_done_answer(written), False, []
    return answer, False, errors


def decode_claude_stream(events: list[dict]) -> tuple[str, bool, list[str]]:
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        return "", False, ['"stream ended without a result event"']
    final = results[-1]
    answer = final.get("result", "")
    denied = bool(final.get("permission_denials"))
    errors = []
    if final.get("is_error"):
        errors.append(json.dumps(final.get("errors", answer)))
    if not answer.strip() and not errors and not denied:
        written = claude_written_files(events)
        if written:
            return work_done_answer(written), False, []
    return answer, denied, errors


FILE_WRITING_TOOLS = ("write_to_file", "replace_file_content", "sed_file", "notebook_edit")


def agy_written_files(events: list[dict]) -> list[str]:
    """Files the run actually modified, read from its own tool events."""
    written: list[str] = []
    for event in events:
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("state") != "DONE":
            continue
        if step.get("tool_name") not in FILE_WRITING_TOOLS:
            continue
        info = step.get("tool_info")
        params = info.get("parameters") if isinstance(info, dict) else None
        target = None
        if isinstance(params, dict):
            for key in ("TargetFile", "AbsolutePath", "FilePath", "Path"):
                value = params.get(key)
                if isinstance(value, str) and value:
                    target = value
                    break
        written.append(target or step["tool_name"])
    return written


def decode_agy_stream(events: list[dict]) -> tuple[str, bool, list[str]]:
    results = [event.get("result", {}) for event in events if event.get("event") == "result"]
    if not results:
        return "", False, ['"stream ended without a result event"']
    final = results[-1]
    if not isinstance(final, dict):
        return "", False, ['"malformed result event"']
    answer = final.get("response", "")
    denials = final.get("denied_actions") or []
    errors = []
    if final.get("status") != "SUCCESS":
        errors.append(json.dumps(final.get("error", "provider error")))
    # A quota or transport error can arrive as its own event before the result;
    # classification needs its text or a 429 would read as an empty answer.
    for event in events:
        if event.get("event") == "error" or ("error" in event and event.get("event") != "result"):
            errors.append(json.dumps(event.get("error", event)))

    # A denial that only blocked running a command is not a failed attempt when
    # the run already edited files: the work is in the worktree and the
    # orchestrator verifies it there. Discarding it would throw away real work
    # because an optional self-check was refused.
    blocking_denial = any(
        not isinstance(d, dict) or d.get("action") != "command" for d in denials)
    if not blocking_denial and not answer.strip() and not errors:
        # Antigravity spends its single headless turn on tools and often ends
        # without writing a summary. The files it already wrote are the work; a
        # missing closing sentence is not a failed attempt, and discarding the
        # worktree over it throws away everything the run produced.
        written = agy_written_files(events)
        if written:
            listing = "\n".join(f"- {path}" for path in dict.fromkeys(written))
            note = ("" if not denials else
                    "\n\nUna verificacion por comando quedo denegada; la ejecuta el orquestador.")
            return (f"Archivos modificados en el worktree:\n{listing}{note}"), False, []

    return answer, bool(denials), errors


OPENCODE_WRITING_TOOLS = {"write", "edit", "patch", "multiedit", "apply_patch"}


def opencode_written_files(events: list[dict]) -> list[str]:
    written = []
    for event in events:
        part = event.get("part")
        if event.get("type") != "tool_use" or not isinstance(part, dict):
            continue
        state = part.get("state") or {}
        if part.get("tool") not in OPENCODE_WRITING_TOOLS or state.get("status") != "completed":
            continue
        params = state.get("input") or {}
        for key in ("path", "filePath", "file_path"):
            if isinstance(params.get(key), str):
                written.append(params[key])
                break
    return written


def decode_opencode_stream(events: list[dict]) -> tuple[str, bool, list[str]]:
    """`opencode run --format json`: one event per line, the answer split in text parts.

    Text parts arrive with an id; the last version of each id wins. Errors come
    as their own event (`provider.quota` with status 402, `provider.no-route`
    when a model is not enabled for the account). Permissions are auto-approved
    with --auto, so a denial only shows up as a tool part with status error.
    """
    texts: dict[str, str] = {}
    errors: list[str] = []
    denied = False
    for event in events:
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "text" and isinstance(part.get("text"), str):
            texts[part.get("id") or str(len(texts))] = part["text"]
        elif kind == "error":
            errors.append(json.dumps(event.get("error", "provider error")))
        elif kind == "tool_use":
            state = part.get("state") or {}
            if state.get("status") == "error" and "permission" in json.dumps(state).casefold():
                denied = True
    answer = "\n".join(t for t in texts.values() if t.strip())
    if not errors and not answer.strip():
        written = opencode_written_files(events)
        if written:
            listing = "\n".join(f"- {path}" for path in dict.fromkeys(written))
            return f"Archivos modificados en el worktree:\n{listing}", False, []
    return answer, denied, errors


STREAM_DECODERS = {
    "codex-jsonl": decode_codex,
    "opencode-json": decode_opencode_stream,
    "claude-stream": decode_claude_stream,
    "agy-stream": decode_agy_stream,
}


DETAIL_CHARS = 400


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:DETAIL_CHARS] if lines else ""


def decode_response(returncode, stdout, stderr, patterns, output_format):
    """Use provider protocol errors, never echoed prompts/tool output, as evidence."""
    classification, answer, _ = decode_response_detail(returncode, stdout, stderr, patterns, output_format)
    return classification, answer


def decode_response_detail(returncode, stdout, stderr, patterns, output_format):
    """Like decode_response, plus the provider's own words for a failure.

    A line that only says `provider_error` cost a night of guessing on
    2026-09-19; the reason was in the stream all along.
    """
    if output_format == "text":
        kind = classify(returncode, stdout, stderr, patterns)
        return kind, stdout, ("" if kind == "valid" else last_line(stderr) or last_line(stdout))
    try:
        decoder = STREAM_DECODERS.get(output_format)
        if decoder is not None:
            answer, denied, errors = decoder(parse_ndjson(stdout))
        else:
            data = json.loads(stdout)
            errors = []
            denied = False
            if output_format == "agy-json":
                answer = data.get("response", "")
                denied = bool(data.get("denied_actions"))
                if data.get("status") != "SUCCESS":
                    errors.append(json.dumps(data.get("error", "provider error")))
            elif output_format == "claude-json":
                answer = data.get("result", "")
                denied = bool(data.get("permission_denials"))
                if data.get("is_error"):
                    errors.append(json.dumps(data.get("errors", answer)))
            else:
                return "configuration_error", ""
        if not isinstance(answer, str):
            return "invalid_response", "", "la respuesta no es texto"
        if errors:
            kind = classify(returncode, "", "\n".join(errors), patterns)
            detail = "; ".join(errors)[:DETAIL_CHARS]
            return ("provider_error" if kind == "no_valid_response" else kind), "", detail
        if answer.strip():
            return "valid", answer, ""
        if denied:
            return "permission_denied", "", "el proveedor denego acciones y no escribio nada"
        return "no_valid_response", "", "respuesta vacia y ningun archivo escrito"
    except (ValueError, TypeError, AttributeError):
        # A CLI can fail before it initializes its JSON protocol.
        kind = classify(returncode, "", stderr, patterns)
        return ("invalid_response" if kind == "no_valid_response" else kind), "", last_line(stderr) or last_line(stdout)


def link_worktree_dirs(links: dict[str, str], cwd: Path) -> list[str]:
    """Symlink heavy, gitignored directories from the main checkout into the worktree.

    A fresh worktree has no node_modules, so an executor runs `npm install`;
    the packages ship dozens of .gitattributes and Crewplane, which walks the
    whole checkout (without following symlinks), aborts the run. A symlink
    gives the agent `npx tsc` without the install. Created only if the target
    is missing and the source exists.
    """
    made = []
    for relative, source in links.items():
        target = cwd / relative
        src = Path(source)
        if target.is_symlink() or not src.is_dir() or (target.exists() and not target.is_dir()):
            continue
        if target.is_dir() and any(target.iterdir()) and not any(c.is_symlink() for c in target.iterdir()):
            continue  # instalacion real del agente: no mezclar
        try:
            # Directorio real con un symlink por paquete, no un symlink al
            # directorio: `.gitignore` dice `mcp/node_modules/` (solo directorios),
            # asi que un symlink acababa commiteado en el resultado (M03-02,
            # 2026-09-20) y Crewplane prohibe tocar info/exclude para taparlo.
            target.mkdir(parents=True, exist_ok=True)
            for child in src.iterdir():
                link = target / child.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(child, target_is_directory=child.is_dir())
            made.append(relative)
        except OSError:
            continue
    return made


def stream_tokens(events: list[dict], output_format: str) -> dict | None:
    """Input/output/cache tokens reported by the provider's own stream, when it does."""
    tin = tout = cache = 0; found = False
    for event in events:
        if output_format == "agy-stream" and event.get("event") == "result":
            usage = (event.get("result") or {}).get("usage") or {}
            if usage:
                found = True; tin += usage.get("input_tokens", 0) or 0; tout += usage.get("output_tokens", 0) or 0
                if not tin and usage.get("total_tokens"): tin += usage["total_tokens"]
        elif output_format == "claude-stream" and event.get("type") == "result":
            usage = event.get("usage") or {}
            if usage:
                found = True; tin += usage.get("input_tokens", 0); tout += usage.get("output_tokens", 0)
                cache += usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        elif output_format == "codex-jsonl" and event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            if usage:
                found = True; tin += usage.get("input_tokens", 0); tout += usage.get("output_tokens", 0); cache += usage.get("cached_input_tokens", 0)
        elif output_format == "opencode-json" and event.get("type") == "step_finish":
            tokens = (event.get("part") or {}).get("tokens") or {}
            if tokens:
                found = True; tin += tokens.get("input", 0); tout += tokens.get("output", 0) + tokens.get("reasoning", 0)
                c = tokens.get("cache") or {}; cache += (c.get("read", 0) + c.get("write", 0)) if isinstance(c, dict) else 0
    return {"in": tin, "out": tout, "cache": cache} if found else None


def append_log(path: Path, role: str, attempt: Attempt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role": role,
        "provider": attempt.provider,
        "classification": attempt.classification,
        "exit_code": attempt.exit_code,
        "duration_ms": attempt.duration_ms,
    }
    if attempt.detail:
        event["detail"] = attempt.detail
    if attempt.tokens:
        event["tokens"] = attempt.tokens
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


HEARTBEAT_SECONDS = 15.0
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
PROGRESS_LINE_CHARS = 120


def describe_message(event: dict) -> str:
    """Name what an assistant/user turn actually did, not just that it happened."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_use":
            name = block.get("name")
            return f"tool:{name}" if isinstance(name, str) else "tool"
        if kind == "tool_result":
            return "tool_result"
        if kind == "thinking":
            return "thinking"
        if kind == "text":
            return "text"
    return ""


def summarize_progress(line: str) -> str:
    """Condense one output line into a progress label.

    The raw payload is not echoed: provider output is JSON and re-emitting it
    would duplicate every answer inside the Crewplane log and inflate its
    visible-text estimate.
    """
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped[:PROGRESS_LINE_CHARS]
    if not isinstance(event, dict):
        return stripped[:PROGRESS_LINE_CHARS]

    # codex nests the interesting part under item; claude under message; agy
    # keys its envelope with "event" instead of "type".
    head = event.get("type") or event.get("event")
    if not isinstance(head, str) or not head:
        return f"json:{len(stripped)}b"
    detail = ""
    item = event.get("item")
    step = event.get("step_update")
    if isinstance(step, dict) and isinstance(step.get("tool_name"), str):
        # Antigravity nests the interesting part here; without it every event
        # collapses to "step_update" and the log says nothing about what ran.
        detail = step["tool_name"]
        if step.get("state") and step["state"] != "DONE":
            detail = f"{detail}:{step['state'].lower()}"
    elif isinstance(item, dict) and isinstance(item.get("type"), str):
        detail = item["type"]
    elif isinstance(event.get("part"), dict):
        # opencode: {"type":"tool_use","part":{"tool":"write","state":{"status":..}}}
        part = event["part"]
        detail = part.get("tool") or part.get("type") or ""
        status = (part.get("state") or {}).get("status")
        if detail and status and status != "completed":
            detail = f"{detail}:{status}"
    elif isinstance(event.get("subtype"), str):
        detail = event["subtype"]
    else:
        detail = describe_message(event)
    return f"{head}:{detail}" if detail else head


class StreamPump:
    """Read a child pipe line by line, keeping the text and reporting progress."""

    def __init__(self, provider: str, stream, announce: bool) -> None:
        self._provider = provider
        self._stream = stream
        self._announce = announce
        self._chunks: list[str] = []
        self._last_label = ""
        self._repeat = 0
        self.last_output = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in iter(self._stream.readline, ""):
                self._chunks.append(line)
                self.last_output = time.monotonic()
                if self._announce:
                    self._announce_label(summarize_progress(line))
            if self._announce:
                self._flush_repeat()
        finally:
            self._stream.close()

    def _announce_label(self, label: str) -> None:
        """Collapse consecutive identical labels so hook chatter stays readable."""
        if not label:
            return
        if label == self._last_label:
            self._repeat += 1
            return
        self._flush_repeat()
        self._last_label = label
        print(f"agent-fallback: {self._provider}: {label}", file=sys.stderr, flush=True)

    def _flush_repeat(self) -> None:
        if self._repeat:
            print(
                f"agent-fallback: {self._provider}: {self._last_label} +{self._repeat} mas",
                file=sys.stderr,
                flush=True,
            )
            self._repeat = 0

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def resolve_placeholder(command: list[str], token: str, value: str | None) -> list[str]:
    """Fill a placeholder such as {model} or {effort}, or drop the flag carrying it.

    Each CLI wants its flags in a different position (codex after `exec`,
    antigravity before its trailing `-p`), so placeholders live in the configured
    command instead of being appended blindly. Without a value, the placeholder
    and the flag immediately before it disappear and the CLI default applies.
    """
    if value:
        return [part.replace(token, value) for part in command]
    resolved: list[str] = []
    for part in command:
        if token in part:
            if resolved:
                resolved.pop()
            continue
        resolved.append(part)
    return resolved


def resolve_model(command: list[str], model: str | None) -> list[str]:
    return resolve_placeholder(command, "{model}", model)


def feed_stdin(process: subprocess.Popen[str], payload: str) -> None:
    """Write the prompt from its own thread so a chatty child cannot deadlock us."""
    def write() -> None:
        try:
            if payload:
                process.stdin.write(payload)
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

    threading.Thread(target=write, daemon=True).start()


def wait_with_heartbeat(process: subprocess.Popen[str], pumps: list[StreamPump], provider: str, deadline: float | None, idle_timeout: float | None = None) -> str:
    """Wait for exit, emitting liveness while the provider stays quiet.

    Returns "exited", "deadline" or "idle". An agent that keeps emitting events
    is working, however long it takes; one that has gone silent is the only kind
    worth killing early. A wall clock alone cannot tell those apart, and cutting
    a working agent loses the whole attempt.
    """
    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return "deadline"
        # Poll no coarser than the shortest thing we must react to, or an idle
        # process would survive until the next heartbeat tick.
        slice_seconds = HEARTBEAT_SECONDS
        if remaining is not None:
            slice_seconds = min(slice_seconds, remaining)
        if idle_timeout is not None:
            slice_seconds = min(slice_seconds, idle_timeout)
        slice_seconds = max(slice_seconds, 0.05)
        try:
            process.wait(timeout=slice_seconds)
            return "exited"
        except subprocess.TimeoutExpired:
            quiet = time.monotonic() - max(pump.last_output for pump in pumps)
            if idle_timeout is not None and quiet >= idle_timeout:
                print(
                    f"agent-fallback: {provider}: sin salida hace {int(quiet)}s, se considera colgado",
                    file=sys.stderr,
                    flush=True,
                )
                return "idle"
            if quiet >= HEARTBEAT_SECONDS:
                print(
                    f"agent-fallback: {provider}: sin salida hace {int(quiet)}s, sigue vivo",
                    file=sys.stderr,
                    flush=True,
                )


def run_provider(provider: str, command: list[str], prompt: str, timeout: float, patterns: dict[str, list[str]], prompt_transport: str = "stdin", output_format: str = "text", idle_timeout: float | None = None, model: str | None = None, effort: str | None = None) -> Attempt:
    started = time.monotonic()
    try:
        # Crewplane materializes a fresh worktree per run and invokes us with it
        # as the working directory. A CLI that resolves file tools against its
        # own workspace instead of the process cwd needs that path passed in, so
        # the command may carry a {cwd} placeholder.
        command = [part.replace("{cwd}", os.getcwd()) for part in command]
        command = resolve_model(command, model)
        command = resolve_placeholder(command, "{effort}", effort)
        if prompt_transport == "argument":
            invocation, supplied_input = [*command, prompt], ""
        elif prompt_transport == "stdin":
            invocation, supplied_input = command, prompt
        else:
            return Attempt(provider, "temporary", 2, "", f"unsupported prompt transport: {prompt_transport}")
        process = subprocess.Popen(
            invocation,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        feed_stdin(process, supplied_input)
        out_pump = StreamPump(provider, process.stdout, announce=True)
        err_pump = StreamPump(provider, process.stderr, announce=False)
        pumps = [out_pump, err_pump]
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            outcome = wait_with_heartbeat(process, pumps, provider, deadline, idle_timeout)
        except KeyboardInterrupt:
            kill_process_group(process)
            for pump in pumps:
                pump.join(1.0)
            raise
        if outcome != "exited":
            kill_process_group(process)
            for pump in pumps:
                pump.join(1.0)
            classification = "timeout" if outcome == "deadline" else "idle_timeout"
            return Attempt(provider, classification, 124, out_pump.text, err_pump.text, round((time.monotonic() - started) * 1000),
                           raw=out_pump.text)
        for pump in pumps:
            pump.join(5.0)
        stdout, stderr = out_pump.text, err_pump.text
        classification, answer, detail = decode_response_detail(process.returncode, stdout, stderr, patterns, output_format)
        try:
            tokens = stream_tokens(parse_ndjson(stdout), output_format) if output_format in STREAM_DECODERS or output_format == "claude-stream" else None
        except (ValueError, TypeError, AttributeError):
            tokens = None
        return Attempt(provider, classification, process.returncode, answer, "",
                       round((time.monotonic() - started) * 1000), detail, tokens, stdout)
    except FileNotFoundError as error:
        return Attempt(provider, "temporary", 127, "", str(error), round((time.monotonic() - started) * 1000))
    except OSError as error:
        return Attempt(provider, "temporary", 126, "", str(error), round((time.monotonic() - started) * 1000))


def kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


DEFAULT_COOLDOWN_SECONDS = 1800.0
COOLING_CLASSIFICATIONS = ("quota", "auth")


def cooldown_path(config: dict[str, Any]) -> Path:
    configured = config.get("cooldown_path")
    if configured:
        return Path(configured)
    return Path(config.get("log_path", ".agent/logs/agent-fallback.jsonl")).parent / "cooldowns.json"


def read_cooldowns(path: Path) -> dict[str, float]:
    """Return provider -> epoch seconds until which it should be skipped.

    A missing or corrupt file means no cooldowns: never let bookkeeping state
    stop the dispatcher from trying a provider.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for provider, entry in data.items():
        until = entry.get("until") if isinstance(entry, dict) else None
        if isinstance(until, (int, float)):
            result[provider] = float(until)
    return result


RESET_PATTERNS = (
    # Codex: "try again at Sep 20th, 2026 4:33 PM" (hora local)
    re.compile(r"try again at ([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? (\d{1,2}):(\d{2})\s?([AaPp][Mm])"),
)
RESET_CLOCK = re.compile(r"(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s?([AaPp][Mm])")
RESET_EPOCH = re.compile(r"\|(\d{10})\b")
# Antigravity: "Resets in 2h30m42s"
RESET_DURATION = re.compile(r"resets? in (?:(\d+)\s*h(?:rs?|ours?)?)?\s*(?:(\d+)\s*m(?:in(?:s|utes?)?)?)?\s*(?:(\d+)\s*s(?:ec(?:s|onds?)?)?)?", re.IGNORECASE)
MAX_RESET_SECONDS = 48 * 3600.0


def reset_seconds(detail: str, now: float | None = None) -> float | None:
    """Seconds until the reset the provider itself announced, if it said when.

    Codex on 2026-09-19 answered "try again at Sep 20th, 2026 4:33 PM": 26
    hours. A fixed 30-minute cooldown would have retried it 52 times for
    nothing and shown it as almost free in every status line.
    """
    if not detail:
        return None
    now = time.time() if now is None else now
    target = None
    match = RESET_PATTERNS[0].search(detail)
    if match:
        month, day, year, hour, minute, meridian = match.groups()
        try:
            parsed = time.strptime(f"{month} {day} {year} {hour}:{minute} {meridian.upper()}", "%b %d %Y %I:%M %p")
            target = time.mktime(parsed)
        except ValueError:
            target = None
    if target is None:
        match = RESET_DURATION.search(detail)
        if match and any(match.groups()):
            hours, minutes, seconds = (int(g or 0) for g in match.groups())
            target = now + hours * 3600 + minutes * 60 + seconds
    if target is None:
        match = RESET_EPOCH.search(detail)
        if match:
            target = float(match.group(1))
    if target is None:
        match = RESET_CLOCK.search(detail)
        if match:
            hour, minute, meridian = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower()
            hour = hour % 12 + (12 if meridian == "pm" else 0)
            local = time.localtime(now)
            target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1))
            if target <= now:
                target += 24 * 3600
    if target is None:
        return None
    return max(0.0, min(target - now, MAX_RESET_SECONDS))


def read_cooldown_reasons(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: str(v.get("reason", "cuota")) for k, v in data.items() if isinstance(v, dict)}


def rotate_first(order: list[str], count: int, path: Path, role: str) -> list[str]:
    """Start each invocation at a different one of the first `count` providers.

    Several boards run at once and all would hit the first provider, draining
    its pool while the others sit idle; rotating the head spreads the load
    across pools (Gemini, DeepSeek, Codex) so they exhaust later and together.
    """
    if count <= 1 or len(order) < 2:
        return order
    head, tail = order[:count], order[count:]
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    turn = int(state.get(role, 0)) % len(head)
    state[role] = turn + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return head[turn:] + head[:turn] + tail


def recent_invocations(log_file: Path, provider: str, window_seconds: float) -> int:
    """Real invocations (not cooling skips) of a provider in the last window, from the jsonl log."""
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return 0
    cutoff = time.time() - window_seconds
    count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("provider") != provider or event.get("classification") == "cooling_down":
            continue
        try:
            stamp = time.mktime(time.strptime(event["timestamp"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except (KeyError, ValueError):
            continue
        if stamp >= cutoff:
            count += 1
    return count


def quota_pool_siblings(providers: dict[str, Any], provider: str) -> list[str]:
    """The provider itself plus every other entry declaring the same quota_pool."""
    pool = providers.get(provider, {}).get("quota_pool")
    if not pool:
        return [provider]
    return [name for name, cfg in providers.items() if cfg.get("quota_pool") == pool] or [provider]


def write_cooldown(path: Path, provider: str, classification: str, seconds: float) -> None:
    """Record a cooldown, replacing the file atomically.

    Several agents can invoke the dispatcher at once, so a partially written
    file must never be observable.
    """
    entries = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            entries = existing
    except (OSError, json.JSONDecodeError):
        entries = {}
    entries[provider] = {
        "until": time.time() + seconds,
        "reason": classification,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError:
        return


RETRY_ANYWAY_WINDOW_SECONDS = 10 * 60.0


def cooling_providers(order: list[str], cooldowns: dict[str, float], now: float) -> set[str]:
    """Providers to skip.

    When every option is cooling, the one closest to its reset is still tried
    if that reset is under ten minutes away (a cooldown is an estimate). Farther
    than that, everything is skipped and the caller reports the wait: on
    2026-09-19 three boards retried Antigravity every five minutes for two
    hours, 150 s of quota error each time, because "attempting a wall beats
    doing nothing" also meant hammering the provider.
    """
    cooling = {provider for provider in order if cooldowns.get(provider, 0.0) > now}
    if cooling and len(cooling) == len(set(order)):
        soonest = min(cooling, key=lambda p: cooldowns[p])
        if cooldowns[soonest] - now <= RETRY_ANYWAY_WINDOW_SECONDS:
            cooling.discard(soonest)
    return cooling


# Notas que run-board anexa al plan cuando un intento fracasa de verdad (no las
# interrupciones por reinicio). El plan viaja dentro del prompt del ejecutor.
HARD_FAILURE_MARKERS = ("## Fallo de `make test`", "## Hallazgos de la revision anterior", "## Conflicto al fusionar")


def hard_failures(prompt: str) -> int:
    return sum(prompt.count(marker) for marker in HARD_FAILURE_MARKERS)


def escalated_order(order: list[str], role: str, prompt: str, config: dict[str, Any]) -> list[str]:
    """Poner delante un modelo mas fuerte cuando la tarea ya fracaso varias veces.

    Pedido de la persona propietaria el 2026-09-29: «si hay un error muy choto
    coloca a sonnet 5.5 a resolverlo». `escalation.<rol>` = {after_failures, providers}.
    """
    rule = config.get("escalation", {}).get(role)
    if not rule or hard_failures(prompt) < int(rule.get("after_failures", 2)):
        return order
    known = config.get("providers", {})
    front = [p for p in rule.get("providers", []) if p in known]
    if front:
        print(f"agent-fallback: {hard_failures(prompt)} fallos previos en la tarea; escalo a {', '.join(front)}",
              file=sys.stderr, flush=True)
    return list(dict.fromkeys(front + order))



TRAIL_KEYS = ("text", "command", "cmd", "file_path", "path", "filePath", "description", "query", "pattern")


def attempt_trail(raw: str, limit: int = 4000) -> str:
    """What a cut-off attempt said and did, read from its stream output.

    The model's context window cannot move to another provider, but its trail can:
    its messages, the commands it ran and the files it touched, in order. Works on
    any NDJSON stream (Claude, Codex, OpenCode, Antigravity) by collecting the
    string fields that carry that meaning; plain-text output is kept as is.
    """
    lines: list[str] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key in TRAIL_KEYS and node.strip():
            text = " ".join(node.split())
            entry = f"{key}: {text[:300]}"
            if not lines or lines[-1] != entry:
                lines.append(entry)

    parsed_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            walk(json.loads(line))
            parsed_any = True
        except ValueError:
            continue
    if not parsed_any:
        lines = [" ".join(raw.split())[-limit:]] if raw.strip() else []
    trail = "\n".join(lines)
    return trail[-limit:]


def worktree_changes(cwd: Path | None = None) -> set[str]:
    """Paths `git status` reports as changed in the agent's worktree (empty outside git)."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=cwd or Path.cwd(),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {line[3:].strip() for line in out.stdout.splitlines() if len(line) > 3}


def continuation_note(failed: list["Attempt"], changed: set[str]) -> str:
    """Tell the next provider that a cut-off attempt left work in the worktree.

    A provider that dies mid-round (quota, transport, timeout) leaves its edits
    on disk, and the next one in the chain gets the same prompt in the same
    worktree. Without this note it may start over and overwrite them
    (2026-10-01: Kimi and GLM hit quota 32 times mid-round).
    """
    who = ", ".join(f"{a.provider} ({a.classification})" for a in failed)
    listing = "\n".join(f"- {path}" for path in sorted(changed)[:60])
    return ("\n\n## Continuacion de un intento cortado\n\n"
            f"Un intento anterior de esta misma ronda se corto: {who}. Dejo estos cambios en el worktree:\n\n"
            f"{listing}\n\nRevisalos con `git status` y `git diff` y CONTINUA desde ellos: no empieces de cero "
            "ni los descartes. Corrige lo que este mal y completa lo que falte.\n"
            + "".join(f"\n### Bitacora de {a.provider} antes de cortarse (lo ultimo que dijo e hizo)\n\n```text\n{trail}\n```\n"
                      for a in failed[-2:] if (trail := attempt_trail(a.raw))))


def snapshot_worktree(cwd: Path, label: str) -> str | None:
    """Pin the worktree's uncommitted state to `salvage/auto/<label>` without touching it.

    Uses a throwaway index, so neither the worktree nor its index changes. The
    branch lives in the shared object store, so it survives Crewplane deleting
    the checkout after a failed node.
    """
    env = dict(os.environ, GIT_INDEX_FILE=str(Path(os.environ.get("TMPDIR", "/tmp")) / f"agent-fallback-{os.getpid()}.idx"))
    def git(*args, **kw):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, **kw)
    try:
        if git("read-tree", "HEAD", env=env).returncode != 0 or git("add", "-A", ".", env=env).returncode != 0:
            return None
        tree = git("write-tree", env=env).stdout.strip()
        head = git("rev-parse", "HEAD").stdout.strip()
        if not tree or not head or tree == git("rev-parse", "HEAD^{tree}").stdout.strip():
            return None
        sha = git("commit-tree", tree, "-p", head, "-m", f"agent-fallback: trabajo parcial ({label})").stdout.strip()
        branch = f"salvage/auto/{re.sub(r'[^A-Za-z0-9._-]+', '-', label)[:80]}-{time.strftime('%Y%m%d-%H%M%S')}"
        if sha and git("branch", "-f", branch, sha).returncode == 0:
            return branch
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        Path(env["GIT_INDEX_FILE"]).unlink(missing_ok=True)
    return None

def dispatch(role: str, prompt: str, config: dict[str, Any], timeout_override: float | None = None, observer=None) -> tuple[Attempt | None, list[Attempt]]:
    timeout = timeout_override if timeout_override is not None else float(config.get("timeout_seconds", 300))
    providers = config["providers"]
    attempts: list[Attempt] = []
    order = list(dict.fromkeys(config.get("role_order", ROLE_ORDER)[role]))
    state_path = cooldown_path(config)
    order = rotate_first(order, int(config.get("rotate_first", {}).get(role, 0)), state_path.parent / "rotation.json", role)
    order = escalated_order(order, role, prompt, config)
    cooldowns = read_cooldowns(state_path)
    cooldown_reasons = read_cooldown_reasons(state_path)
    now = time.time()
    cooling = cooling_providers(order, cooldowns, now)
    cooldown_seconds = float(config.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
    idle_raw = config.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
    idle_timeout = None if idle_raw is None else float(idle_raw)
    role_models = config.get("role_models", {}).get(role, {})
    role_efforts = config.get("role_efforts", {}).get(role, {})
    budgets = config.get("hourly_budget", {})
    log_file = Path(config.get("log_path", ".agent/logs/agent-fallback.jsonl"))
    initial_changes = worktree_changes()
    for provider in order:
        provider_config = providers[provider]
        if not provider_config.get("enabled", True):
            attempts.append(Attempt(provider, "disabled", 0))
            continue
        cap = budgets.get(provider)
        if cap and recent_invocations(log_file, provider, 3600) >= int(cap):
            # Presupuesto por hora: se usa, pero despacio. Pedido para Claude el
            # 2026-09-20: que no se agote en una manana de revisiones.
            attempt = Attempt(provider, "cooling_down", 0)
            attempts.append(attempt)
            print(f"agent-fallback: {provider}: tope de {cap} invocaciones/hora alcanzado, se omite",
                  file=sys.stderr, flush=True)
            if observer:
                observer(attempt)
            continue
        if provider in cooling:
            attempt = Attempt(provider, "cooling_down", 0)
            attempts.append(attempt)
            # Sin duracion ni hora: Crewplane toma la MAYOR espera que encuentre
            # en la salida y con mas de 5 h tumba el nodo. El 2026-09-19 la
            # espera de 23 h de Codex, impresa aqui, cerro una ronda de M01-04
            # cuando Antigravity volvia en dos horas.
            print(
                f"agent-fallback: {provider}: en enfriamiento por {cooldown_reasons.get(provider, 'cuota')}, se omite",
                file=sys.stderr,
                flush=True,
            )
            if observer:
                observer(attempt)
            continue
        failed_real = [a for a in attempts if a.classification not in ("cooling_down", "disabled", "valid")]
        changed = worktree_changes() - initial_changes if failed_real else set()
        attempt = run_provider(
            provider,
            provider_config["command"],
            prompt + continuation_note(failed_real, changed) if changed else prompt,
            timeout,
            provider_config.get("patterns", {}),
            provider_config.get("prompt_transport", "stdin"),
            provider_config.get("output_format", "text"),
            idle_timeout,
            role_models.get(provider),
            role_efforts.get(provider),
        )
        attempts.append(attempt)
        if attempt.classification in COOLING_CLASSIFICATIONS and cooldown_seconds > 0:
            announced = reset_seconds(attempt.detail) if attempt.classification == "quota" else None
            seconds = max(cooldown_seconds, announced or 0.0)
            # Dos entradas del mismo CLI con modelos distintos pueden compartir
            # la bolsa de cuota (Codex Sol y Terra; Gemini Pro y Flash). Enfriar
            # solo la que fallo haria probar a la hermana para nada.
            for sibling in quota_pool_siblings(providers, provider):
                write_cooldown(state_path, sibling, attempt.classification, seconds)
        if observer:
            observer(attempt)
        if attempt.classification == "valid":
            return attempt, attempts
    return None, attempts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # El rol se valida contra el fichero de configuracion, no contra la constante
    # del codigo: role_order es editable y anadir un rol ahi no debe exigir
    # tocar el dispatcher.
    parser.add_argument("--role", required=True)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("AGENT_FALLBACK_CONFIG", str(Path(__file__).resolve().parent / "config/agent-fallback.json"))))
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument(
        "--on-exhausted", choices=("fail", "report"), default="fail",
        help="que hacer si ningun proveedor responde: fallar, o informarlo como "
             "resultado vacio y salir con exito")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("agent-fallback: prompt vacío en stdin", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except (OSError, json.JSONDecodeError) as error:
        print(f"agent-fallback: configuración inválida: {error}", file=sys.stderr)
        return 2
    known_roles = config.get("role_order", ROLE_ORDER)
    if args.role not in known_roles:
        print(f"agent-fallback: rol desconocido '{args.role}'; definidos: "
              f"{', '.join(sorted(known_roles))}", file=sys.stderr)
        return 2
    log_path = Path(config.get("log_path", ".agent/logs/agent-fallback.jsonl"))
    def report(attempt):
        append_log(log_path, args.role, attempt)
        line = f"agent-fallback: {attempt.provider}: {attempt.classification} (exit={attempt.exit_code}, {attempt.duration_ms}ms)"
        if attempt.detail and attempt.classification != "valid":
            line += f" — {attempt.detail}"
        print(line, file=sys.stderr, flush=True)
    link_worktree_dirs(config.get("worktree_links", {}), Path.cwd())
    selected, attempts = dispatch(args.role, prompt, config, args.timeout_seconds, observer=report)
    if selected is not None:
        sys.stdout.write(selected.stdout)
        return selected.exit_code
    summary = ", ".join(f"{item.provider}={item.classification}" for item in attempts)
    print(f"agent-fallback: todos los proveedores fallaron; {summary}", file=sys.stderr)
    if worktree_changes():
        # El nodo va a fallar y Crewplane descarta el checkout: lo editado se perderia.
        kept = snapshot_worktree(Path.cwd(), f"{args.role}-{Path.cwd().parent.parent.name}")
        if kept:
            print(f"agent-fallback: trabajo parcial guardado en {kept}", file=sys.stderr, flush=True)
    if all(item.classification in ("quota", "cooling_down", "disabled") for item in attempts):
        # Crewplane distingue "cuota" de "fallo" por el texto: con esta linea
        # reintenta cada pocos minutos en vez de tumbar el nodo y perder la ronda.
        cooling = read_cooldowns(cooldown_path(config))
        order = config.get("role_order", ROLE_ORDER).get(args.role, ())
        release = min((cooling[p] for p in order if p in cooling), default=0.0)  # el primero en volver
        wait = max(0, int(release - time.time()))
        # Formato de duracion, que Crewplane parsea; una hora del reloj la
        # leeria como "manana" cuando ya paso.
        print(f"agent-fallback: quota reached for every provider of role {args.role}; "
              f"resets in {wait // 3600}h {(wait % 3600) // 60}m "
              f"({time.strftime('%H:%M', time.localtime(release)) if release else '?'})", file=sys.stderr)
    if args.on_exhausted == "report":
        # Un rol que compite con otros en paralelo no debe tumbar el flujo por no
        # producir nada: eso es un candidato vacio, y quien orquesta ya distingue
        # "sin cambios" de "con cambios". Fallar aqui bloquea a los que si
        # trabajaron, porque un nodo dependiente exige que todos hayan tenido exito.
        sys.stdout.write(
            f"Sin resultado: ningun proveedor produjo una respuesta valida ({summary}).\n"
            f"No hay cambios que revisar en este worktree.\n")
        return 0
    return next((item.exit_code for item in reversed(attempts) if item.exit_code), 1)


if __name__ == "__main__":
    raise SystemExit(main())
