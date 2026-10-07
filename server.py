#!/usr/bin/env python3
"""Control Room: a live, read-only view of Claude Code sessions, sub-agents and workflow runs.

Reads the session logs in ~/.claude/projects. Serves http://127.0.0.1:4317 (CONTROL_ROOM_PORT to change).
Run: python3 ~/.claude/control-room/server.py
"""
import glob
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECTS = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CONTROL_ROOM_PORT", "4317"))
WINDOW_HOURS = 12
PHASE_BY_TYPE = {"planner": "Plan", "Plan": "Plan", "implementer": "Implement", "reviewer": "Review", "Explore": "Research"}

_cache = {}  # path -> (mtime, size, parsed rows)


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def rows(path):
    """Parsed JSONL rows, re-read only when the file changes."""
    stat = os.stat(path)
    hit = _cache.get(path)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]
    parsed = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                pass  # a line still being written
    _cache[path] = (stat.st_mtime, stat.st_size, parsed)
    return parsed


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def tool_target(block):
    data = block.get("input") or {}
    for key in ("file_path", "path", "pattern", "url", "query", "description", "skill", "subagent_type"):
        if data.get(key):
            value = str(data[key])
            return os.path.basename(value) if key in ("file_path", "path") else value[:70]
    if data.get("command"):
        return data["command"].strip().splitlines()[0][:70]
    return ""


def turn_entries(entries):
    return [e for e in entries if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict)]


def blocks(entry):
    content = entry["message"].get("content")
    return content if isinstance(content, list) else []


def summarize_log(entries):
    """Model, tokens, timing, last activity and events of one transcript."""
    model, context = None, 0
    events, last_text, branch, cwd = [], "", None, None
    for entry in entries:
        branch = entry.get("gitBranch") or branch
        cwd = entry.get("cwd") or cwd
        if entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
            continue
        msg = entry["message"]
        model = msg.get("model") or model
        usage = msg.get("usage") or {}  # output_tokens in the log is partial (streaming), so only context is shown
        context = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) or context
        for block in blocks(entry):
            if block.get("type") == "tool_use":
                events.append({"t": ts(entry["timestamp"]), "tool": block.get("name"), "target": tool_target(block)})
            elif block.get("type") == "text" and block.get("text", "").strip():
                last_text = block["text"].strip()
    stamps = [ts(e["timestamp"]) for e in entries if e.get("timestamp")]
    return {
        "model": model,
        "context": context,
        "events": events,
        "lastText": last_text,
        "branch": branch,
        "cwd": cwd,
        "start": min(stamps) if stamps else 0,
        "last": max(stamps) if stamps else 0,
    }


def agent_status(entries, summary, finished, now):
    turns = turn_entries(entries)
    if finished or any(b.get("name") == "SubagentHandback" for e in turns[-2:] for b in blocks(e)):
        return "done"
    quiet = now - summary["last"]
    last = turns[-1] if turns else None
    if last and last["type"] == "assistant" and not any(b.get("type") == "tool_use" for b in blocks(last)) and quiet > 10:
        return "done"
    return "stalled" if quiet > 300 else "running"


def session_status(entries, now):
    turns = turn_entries(entries)
    if not turns:
        return "idle", 0
    last = turns[-1]
    quiet = now - ts(last.get("timestamp"))
    has_tool = any(b.get("type") == "tool_use" for b in blocks(last))
    if last["type"] == "assistant" and not has_tool:
        return ("needs-you" if quiet < 6 * 3600 else "idle"), quiet
    if last["type"] == "assistant" and has_tool and quiet > 45:
        return "check", quiet  # a tool waits: a permission prompt or a long command
    return ("idle" if quiet > 1800 else "working"), quiet


def workflow_runs(session_dir):
    runs = {}
    for path in glob.glob(os.path.join(session_dir, "workflows", "wf_*.json")):
        data = read_json(path)
        script = data.get("script", "")
        name = re.search(r"name:\s*['\"]([^'\"]+)", script)
        runs[data.get("runId") or os.path.basename(path)[:-5]] = {
            "id": data.get("runId"),
            "name": name.group(1) if name else "workflow",
            "phases": re.findall(r"title:\s*['\"]([^'\"]+)", script),
            "start": ts(data.get("timestamp")),
        }
    return runs


def agents_for(session_dir, now):
    agents, runs = [], workflow_runs(session_dir)
    journals = {}
    for journal in glob.glob(os.path.join(session_dir, "subagents", "workflows", "wf_*", "journal.jsonl")):
        run_id = os.path.basename(os.path.dirname(journal))
        for row in rows(journal):
            if row.get("agentId"):
                info = journals.setdefault(row["agentId"], {"run": run_id})
                if row.get("type") == "started":
                    info.update(label=row.get("label"), phase=row.get("phase"))
                elif row.get("type") == "result":
                    info["finished"] = True
    for path in glob.glob(os.path.join(session_dir, "subagents", "**", "agent-*.jsonl"), recursive=True):
        agent_id = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        meta = read_json(path[:-len(".jsonl")] + ".meta.json")
        entries = rows(path)
        summary = summarize_log(entries)
        flow = journals.get(agent_id, {})
        kind = meta.get("agentType") or "agent"
        agents.append({
            "id": agent_id,
            "type": kind,
            "label": flow.get("label") or meta.get("description") or kind,
            "phase": flow.get("phase") or meta.get("workflowPhase") or PHASE_BY_TYPE.get(kind, "Other"),
            "run": flow.get("run"),
            "status": agent_status(entries, summary, flow.get("finished"), now),
            "model": summary["model"],
            "calls": len(summary["events"]),
            "context": summary["context"],
            "start": summary["start"],
            "last": summary["last"],
            "activity": summary["events"][-1] if summary["events"] else None,
            "events": summary["events"][-25:],
        })
    agents.sort(key=lambda a: a["start"])
    for run in runs.values():
        members = [a for a in agents if a["run"] == run["id"]]
        run["status"] = "running" if any(a["status"] == "running" for a in members) else "done"
        run["agents"] = len(members)
    return agents, sorted(runs.values(), key=lambda r: r["start"])


def state():
    now = time.time()
    sessions = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if now - os.path.getmtime(path) > WINDOW_HOURS * 3600:
            continue
        entries = rows(path)
        summary = summarize_log(entries)
        status, quiet = session_status(entries, now)
        session_dir = path[:-len(".jsonl")]
        agents, runs = agents_for(session_dir, now) if os.path.isdir(session_dir) else ([], [])
        if any(a["status"] == "running" for a in agents) and status == "idle":
            status = "working"
        feed = [dict(e, who="lead") for e in summary["events"][-40:]]
        for agent in agents:
            feed += [dict(e, who=agent["type"]) for e in agent.pop("events")]
        sessions.append({
            "id": os.path.basename(session_dir),
            "repo": os.path.basename(summary["cwd"] or "") or os.path.basename(os.path.dirname(path)),
            "branch": summary["branch"],
            "status": status,
            "quiet": quiet,
            "model": summary["model"],
            "context": summary["context"],
            "lastText": summary["lastText"][:400],
            "start": summary["start"],
            "last": summary["last"],
            "agents": agents,
            "runs": runs,
            "feed": sorted(feed, key=lambda e: e["t"])[-40:],
        })
    rank = {"needs-you": 0, "check": 1, "working": 2, "idle": 3}
    sessions.sort(key=lambda s: (rank[s["status"]], -s["last"]))
    return {"now": now, "sessions": sessions}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body, kind = json.dumps(state()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as handle:
                body, kind = handle.read(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Control Room: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
