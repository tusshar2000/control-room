#!/usr/bin/env python3
"""Control Room in the terminal: a live mission-control view of Claude Code sessions and workflows.

Keys: up/down or j/k select a session, mouse wheel or PgUp/PgDn scroll the agents, q quits.
Snapshot for checks: tui.py --snapshot 150x42 [--frame N]   (plain text)
                     tui.py --html 150x42 out.html          (colored HTML preview)
"""
import html
import os
import re
import select
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import state  # noqa: E402  same data as the web view

# Palette (24-bit color)
BG, PANEL, PANEL2, CARD = "#070b14", "#0c1322", "#111b30", "#0f1829"
EDGE, FAINT, MUTED, TEXT, WHITE = "#1c2840", "#2b3a55", "#64748b", "#cbd5e1", "#f1f5f9"
CYAN, VIOLET, PINK, BLUE = "#22d3ee", "#a78bfa", "#f472b6", "#60a5fa"
GREEN, LIME, AMBER, ORANGE, RED = "#34d399", "#a3e635", "#fbbf24", "#fb923c", "#f87171"

ORDER = ["Research", "Plan", "Implement", "Review", "Fix", "Other"]
VERB = {"Read": "read", "Edit": "edit", "Write": "write", "Bash": "run", "Grep": "grep", "Glob": "glob", "Agent": "spawn",
        "Skill": "skill", "WebFetch": "fetch", "WebSearch": "search", "SubagentHandback": "hand back", "StructuredOutput": "report"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPARK = " ▁▂▃▄▅▆▇█"
STATUS = {"needs-you": AMBER, "check": ORANGE, "working": CYAN, "running": CYAN, "done": GREEN, "stalled": RED, "idle": MUTED}
ROLE = {"planner": VIOLET, "implementer": CYAN, "reviewer": AMBER, "lead": PINK}
MODEL = {"opus": VIOLET, "sonnet": BLUE, "haiku": LIME, "fable": PINK}


def ago(sec):
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec / 3600:.1f}h".replace(".0h", "h")


def fit(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def model_name(model):
    parts = (model or "").split("-")
    return f"{parts[1].capitalize()} {parts[2]}.{parts[3]}" if len(parts) >= 4 and parts[0] == "claude" else (model or "")


def model_color(model):
    return next((color for family, color in MODEL.items() if family in (model or "")), MUTED)


def mix(a, b, t):
    """Blend two #rrggbb colors; t=0 gives a, t=1 gives b."""
    pa, pb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


class Canvas:
    """A grid of cells [char, fg, bg, bold]; ANSI or HTML draws it."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.cells = [[[" ", TEXT, BG, False] for _ in range(width)] for _ in range(height)]
        self.clip = (0, height)

    def put(self, y, x, text, fg=TEXT, bg=None, bold=False):
        if self.clip[0] <= y < self.clip[1]:
            for i, ch in enumerate(text):
                if 0 <= x + i < self.w:
                    cell = self.cells[y][x + i]
                    cell[0], cell[1], cell[3] = ch, fg, bold
                    if bg:
                        cell[2] = bg

    def fill(self, y, x, w, h, bg):
        for row in range(y, y + h):
            if self.clip[0] <= row < self.clip[1]:
                for col in range(max(0, x), min(self.w, x + w)):
                    self.cells[row][col] = [" ", TEXT, bg, False]

    def gradient(self, y, x, text, a, b, bold=True, bg=None):
        for i, ch in enumerate(text):
            self.put(y, x + i, ch, mix(a, b, i / max(1, len(text) - 1)), bg, bold)

    def chip(self, y, x, text, color):
        """Text on a dark tint of its own color. Returns the width used."""
        self.put(y, x, f" {text} ", color, mix(BG, color, 0.18), True)
        return len(text) + 2

    def text(self):
        return "\n".join("".join(c[0] for c in row).rstrip() for row in self.cells)


def flow_line(c, y, x, length, mode, frame):
    """Horizontal link. 'flow': a bright particle with a fading trail moves right. 'passed': solid. 'wait': faint."""
    for i in range(length):
        if mode == "flow":
            d = (frame - i) % 8  # distance behind the particle head
            ch, fg = ("●", CYAN) if d == 0 else ("━", mix(EDGE, CYAN, max(0.0, 1 - d / 4)))
        elif mode == "passed":
            ch, fg = "━", mix(EDGE, GREEN, 0.55)
        else:
            ch, fg = "┄", FAINT
        c.put(y, x + i, ch, fg)
    c.put(y, x + length, "▶", {"flow": CYAN, "passed": mix(EDGE, GREEN, 0.7)}.get(mode, FAINT))


def drop_line(c, y, x, rows, active, frame):
    for i in range(rows):
        head = active and (frame // 2 - i) % 3 == 0
        c.put(y + i, x, "●" if head else "│", CYAN if head else mix(EDGE, CYAN, 0.4) if active else EDGE)


def lanes_for(session):
    phases = [p for p in ORDER if any(a["phase"] == p for a in session["agents"])]
    extra = [p for p in dict.fromkeys(a["phase"] for a in session["agents"]) if p not in ORDER]
    run = session["runs"][-1] if session["runs"] else None
    declared = run["phases"] if run and run["phases"] else []
    return list(dict.fromkeys(declared + phases + extra)), run


def narrate(session, lanes, run, now):
    """Two plain sentences: what happens now, and the state of the last workflow."""
    running = [a for a in session["agents"] if a["status"] == "running"]
    if running:
        steps = "; ".join(f"{fit(a['label'], 30)} → {VERB.get(a['activity']['tool'], a['activity']['tool'])} {a['activity']['target']}"
                          if a["activity"] else fit(a["label"], 30) for a in running)
        now_line = f"{running[0]['phase']}: {len(running)} agent{'s' if len(running) > 1 else ''} at work. {steps}"
    elif session["status"] == "working":
        last = session["feed"][-1] if session["feed"] else None
        now_line = "The lead works alone." + (f" Last action: {VERB.get(last['tool'], last['tool'])} {last['target']}." if last else "")
    elif session["status"] == "needs-you":
        now_line = f"Nothing runs. The lead waits for your reply ({ago(session['quiet'])})."
    elif session["status"] == "check":
        now_line = "A tool waits. Approve it in that pane, or a long command still runs."
    else:
        now_line = f"Nothing runs. Last activity {ago(now - session['last'])} ago."
    if not run:
        return now_line, "No workflow in this session." + (f" {len(session['agents'])} sub-agents ran." if session["agents"] else "")
    counts = []
    for lane in lanes:
        items = [a for a in session["agents"] if a["phase"] == lane]
        done = sum(a["status"] == "done" for a in items)
        counts.append((f"{lane} skipped" if run["status"] == "done" else f"{lane} not started") if not items else f"{lane} {done}/{len(items)}")
    return now_line, f"{run['name']} {'is running' if run['status'] == 'running' else 'finished'}: " + "  →  ".join(counts)


def header(c, sessions, agents, frame):
    c.fill(0, 0, c.w, 1, PANEL)
    c.put(0, 1, "◢◤", CYAN, PANEL, True)
    c.gradient(0, 4, "C O N T R O L   R O O M", CYAN, VIOLET, bg=PANEL)
    busy = any(a["status"] == "running" for a in agents) or any(s["status"] == "working" for s in sessions)
    pulse = mix(PANEL, CYAN, 0.35 + 0.65 * abs((frame % 16) - 8) / 8) if busy else GREEN
    c.put(0, 30, "●", pulse, PANEL)
    c.put(0, 32, "LIVE" if busy else "IDLE", MUTED, PANEL, True)
    stats = [("active", sum(s["status"] != "idle" for s in sessions), AMBER), ("running", sum(a["status"] == "running" for a in agents), CYAN),
             ("done", sum(a["status"] == "done" for a in agents), GREEN), ("calls", sum(a["calls"] for a in agents), VIOLET)]
    x = c.w - 8 - sum(len(f"{v} {k}") + 4 for k, v, _ in stats)
    for name, value, color in stats:
        c.put(0, x, "◆", color, PANEL)
        c.put(0, x + 2, str(value), WHITE, PANEL, True)
        c.put(0, x + 3 + len(str(value)), name, MUTED, PANEL)
        x += len(f"{value} {name}") + 4
    c.put(0, c.w - 6, time.strftime("%H:%M"), MUTED, PANEL)


def focus_bar(c, y, focus):
    color = AMBER if focus["status"] == "needs-you" else ORANGE
    for x in range(c.w):
        c.put(y, x, " ", TEXT, mix(BG, color, 0.16 * (1 - x / c.w) + 0.03))
    c.put(y, 0, "▌", color)
    label = "LOOK HERE NEXT" if focus["status"] == "needs-you" else "CHECK THIS PANE"
    c.put(y, 2, "▶ " + label, color, None, True)
    where = f"{focus['repo']} · {focus['branch'] or ''}"
    c.put(y, 21, where, WHITE, None, True)
    wait = f"waiting {ago(focus['quiet'])}"
    msg = "a tool waits: approve it, or a long command runs" if color == ORANGE else (focus["lastText"].splitlines() or [""])[0]
    c.put(y, 24 + len(where), fit(msg, c.w - len(where) - len(wait) - 30), MUTED)
    c.put(y, c.w - len(wait) - 2, wait, color, None, True)


def sidebar(c, top, width, sessions, session, now):
    c.fill(top, 0, width, c.h - top - 1, PANEL)
    c.put(top + 1, 2, "S E S S I O N S", MUTED, PANEL, True)
    c.put(top + 1, width - 3 - len(str(len(sessions))), str(len(sessions)), FAINT, PANEL)
    y = top + 3
    for s in sessions:
        if y + 2 > c.h - 2:
            break
        selected = s["id"] == session["id"]
        bg = PANEL2 if selected else PANEL
        c.fill(y, 0, width, 2, bg)
        if selected:
            c.put(y, 0, "▎", CYAN, bg)
            c.put(y + 1, 0, "▎", CYAN, bg)
        c.put(y, 2, "●", STATUS[s["status"]], bg)
        c.put(y, 4, fit(s["repo"], width - 11), WHITE if selected else TEXT, bg, selected)
        c.put(y, width - 6, ago(now - s["last"]).rjust(4), MUTED, bg)
        c.put(y + 1, 4, fit(s["branch"] or "no branch", width - 6), MUTED if selected else FAINT, bg)
        y += 3


def node(c, y, x, w, title, state, frame, count="", progress=None):
    """A phase node: a rounded frame on a panel tint, a title row, an optional gradient progress bar."""
    color = {"running": CYAN, "done": GREEN, "wait": FAINT, "needs-you": AMBER, "working": CYAN, "check": ORANGE, "idle": MUTED}[state]
    if state in ("running", "working"):
        color = mix(mix(EDGE, CYAN, 0.5), CYAN, abs((frame % 12) - 6) / 6)  # the border breathes while it runs
    c.fill(y + 1, x + 1, w - 2, 2, PANEL2)
    c.put(y, x, "╭" + "─" * (w - 2) + "╮", color)
    for row in (1, 2):
        c.put(y + row, x, "│", color)
        c.put(y + row, x + w - 1, "│", color)
    c.put(y + 3, x, "╰" + "─" * (w - 2) + "╯", color)
    icon = SPIN[frame % len(SPIN)] if state in ("running", "working") else {"done": "✓", "wait": "○", "needs-you": "◆", "check": "!", "idle": "○"}[state]
    c.put(y + 1, x + 2, icon, color, PANEL2, True)
    c.put(y + 1, x + 4, title, WHITE if state != "wait" else MUTED, PANEL2, True)
    if count:
        c.put(y + 1, x + w - 2 - len(count), count, MUTED, PANEL2)
    if progress is not None:
        bar = w - 4
        filled = round(bar * progress)
        for i in range(bar):
            c.put(y + 2, x + 2 + i, "━", mix(CYAN, VIOLET, i / max(1, bar - 1)) if i < filled else EDGE, PANEL2)


PHASE = {"Plan": VIOLET, "Implement": CYAN, "Review": AMBER, "Fix": PINK, "Research": BLUE}


def agent_card(c, y, x, w, a, now, frame):
    """A running or stalled agent: flat card, role accent bar, live activity, model chip, activity sparkline."""
    role = ROLE.get(a["type"], BLUE)
    c.fill(y, x, w, 4, CARD)
    for row in range(4):
        c.put(y + row, x, "▌", role, CARD)
    c.put(y, x + 2, a["type"].upper(), role, CARD, True)
    c.chip(y, x + 4 + len(a["type"]), a["phase"], PHASE.get(a["phase"], MUTED))
    icon, icolor = (SPIN[frame % len(SPIN)], CYAN) if a["status"] == "running" else ("! stalled", RED)
    c.put(y, x + w - 1 - len(icon), icon, icolor, CARD, True)
    c.put(y + 1, x + 2, fit(a["label"], w - 3), WHITE, CARD, True)
    act = a["activity"]
    c.put(y + 2, x + 2, "›", CYAN, CARD, True)
    c.put(y + 2, x + 4, fit(f"{VERB.get(act['tool'], act['tool'])} {act['target']}" if act else "starting", w - 5), TEXT, CARD)
    used = c.chip(y + 3, x + 2, model_name(a["model"]) or "model", model_color(a["model"]))
    meta = f"{ago(now - a['start'])} · {a['calls']}"
    c.put(y + 3, x + 3 + used, meta, MUTED, CARD)
    spark = a.get("spark") or []
    room = w - 5 - used - len(meta)
    if spark and room >= 6:
        peak = max(spark) or 1
        line = "".join(SPARK[round(v / peak * 8)] for v in spark[-min(len(spark), room - 1):])
        c.gradient(y + 3, x + w - 1 - len(line), line, mix(CARD, CYAN, 0.5), CYAN, bold=False, bg=CARD)


def done_row(c, y, x, w, a):
    meta = f"{model_name(a['model'])} · {ago(a['last'] - a['start'])} · {a['calls']} call{'' if a['calls'] == 1 else 's'}"
    c.put(y, x, "✓", GREEN, None, True)
    c.put(y, x + 2, "●", PHASE.get(a["phase"], MUTED))
    c.put(y, x + 4, fit(a["label"], w - len(meta) - 7), TEXT)
    c.put(y, x + w - len(meta) - 1, meta, FAINT)


def section(c, y, x, w, title, count, color):
    c.put(y, x, title, color, None, True)
    c.put(y, x + len(title) + 2, str(count), MUTED)
    c.put(y, x + len(title) + len(str(count)) + 4, "─" * max(0, w - len(title) - len(str(count)) - 4), EDGE)


def feed(c, y, x0, fw, session, now):
    c.put(y, x0, "─" * fw, EDGE)
    c.put(y, x0 + 2, " L I V E   F E E D ", MUTED, None, True)
    c.put(y + 1, x0 + 1, "LEAD ›", PINK, None, True)
    c.put(y + 1, x0 + 8, fit(session["lastText"], fw - 9), TEXT)
    for row, e in enumerate(reversed(session["feed"][-4:])):
        line = y + 2 + row
        who = e["who"]
        c.put(line, x0 + 1, ago(now - e["t"]).rjust(4), FAINT)
        c.put(line, x0 + 7, fit(who, 11), ROLE.get(who, BLUE), None, True)
        c.put(line, x0 + 20, VERB.get(e["tool"], e["tool"] or ""), WHITE, None, True)
        c.put(line, x0 + 31, fit(e["target"], fw - 32), MUTED)


def footer(c):
    y = c.h - 1
    c.fill(y, 0, c.w, 1, PANEL)
    x = 1
    for key, label in (("↑↓", "session"), ("wheel", "scroll"), ("q", "quit")):
        c.put(y, x, key, CYAN, PANEL, True)
        c.put(y, x + len(key) + 1, label, MUTED, PANEL)
        x += len(key) + len(label) + 4
    x += 2
    for sample, fg, label in (("●━━▶", CYAN, "data moves now"), ("━━━▶", GREEN, "work passed"), ("┄┄┄▶", FAINT, "not started")):
        c.put(y, x, sample, fg, PANEL)
        c.put(y, x + 5, label, MUTED, PANEL)
        x += len(label) + 9


def draw(snapshot, selected_id, width, height, frame, scroll=0):
    c = Canvas(width, height)
    now, sessions = snapshot["now"], snapshot["sessions"]
    if not sessions:
        c.put(1, 2, "No Claude Code sessions in the last 12 hours.", MUTED)
        return c, None, 0
    session = next((s for s in sessions if s["id"] == selected_id), sessions[0])
    agents = [a for s in sessions for a in s["agents"]]

    header(c, sessions, agents, frame)
    top = 2
    focus = next((s for s in sessions if s["status"] == "needs-you"), None) or next((s for s in sessions if s["status"] == "check"), None)
    if focus:
        focus_bar(c, 2, focus)
        top = 4

    side = 30 if width >= 160 else 26 if width >= 120 else 0
    if side:
        sidebar(c, top, side, sessions, session, now)
    x0 = side + 2 if side else 1
    fw = width - x0 - 1

    # Title row
    lanes, run = lanes_for(session)
    c.put(top, x0, session["repo"], WHITE, None, True)
    c.put(top, x0 + len(session["repo"]) + 2, session["branch"] or "", MUTED)
    status_text = session["status"].replace("-", " ").upper()
    c.chip(top, x0 + len(session["repo"]) + len(session["branch"] or "") + 4, f"● {status_text}", STATUS[session["status"]])
    if run:
        tag = f"⌁ {run['name']} · {run['agents']} agents · {run['status']}"
        c.put(top, x0 + fw - len(tag), tag, CYAN if run["status"] == "running" else MUTED)

    # Narration
    now_line, flow_text = narrate(session, lanes, run, now)
    used = c.chip(top + 2, x0, "NOW ", CYAN if "at work" in now_line else STATUS[session["status"]])
    c.put(top + 2, x0 + used + 1, fit(now_line, fw - used - 1), WHITE)
    used = c.chip(top + 3, x0, "FLOW", VIOLET)
    c.put(top + 3, x0 + used + 1, fit(flow_text, fw - used - 1), TEXT)

    # Nodes and links
    columns = ["LEAD"] + lanes
    gap = 7
    while True:
        bw = min(34, (fw - gap * (len(columns) - 1)) // len(columns))
        if bw >= 17 or len(columns) <= 2:
            break
        columns.pop()  # ponytail: very narrow panes drop the last lanes; widen the pane to see them
    order = {"running": 0, "stalled": 1, "done": 2}
    by_lane = {p: sorted((a for a in session["agents"] if a["phase"] == p), key=lambda a: (order.get(a["status"], 1), a["start"])) for p in lanes}

    def lane_state(name):
        if name == "LEAD":
            return session["status"] if session["status"] in ("working", "needs-you", "check") else "done"
        items = by_lane[name]
        return "running" if any(a["status"] == "running" for a in items) else "done" if items else "wait"

    y_head = top + 5
    feed_top = height - 7
    for i, name in enumerate(columns):
        x = x0 + i * (bw + gap)
        st = lane_state(name)
        if name == "LEAD":
            node(c, y_head, x, bw, "LEAD", st, frame)
            c.put(y_head + 2, x + 2, fit(model_name(session["model"]), bw - 12), model_color(session["model"]), PANEL2, True)
            ctx = f"ctx {session['context'] // 1000}k"
            c.put(y_head + 2, x + bw - 2 - len(ctx), ctx, MUTED, PANEL2)
        else:
            items = by_lane[name]
            done = sum(a["status"] == "done" for a in items)
            node(c, y_head, x, bw, name.upper(), st, frame, f"{done}/{len(items)}" if items else "", (done / len(items)) if items else 0.0)
            if not items:
                label = "skipped" if run and run["status"] == "done" else "waiting"
            else:
                label = f"{sum(a['status'] == 'running' for a in items)} running" if st == "running" else "complete"
            c.put(y_head + 4, x + 2, label, CYAN if st == "running" else FAINT)
        if i:
            prev, target = lane_state(columns[i - 1]), lane_state(name)
            mode = "flow" if target in ("running", "working") else "passed" if target == "done" and prev != "wait" else "wait"
            flow_line(c, y_head + 1, x - gap + 1, gap - 3, mode, frame)

    # Middle: active agents as wide cards, then completed agents in columns. The whole area scrolls.
    area_top, area_bottom = y_head + 6, feed_top - 1
    active = [a for a in session["agents"] if a["status"] != "done"]
    finished = sorted((a for a in session["agents"] if a["status"] == "done"), key=lambda a: -a["last"])
    c.clip = (area_top, area_bottom)
    y = area_top - scroll
    if active:
        section(c, y, x0, fw, "A C T I V E   A G E N T S", len(active), CYAN)
        y += 2
        cols = max(1, min(3, fw // 48))
        cw = (fw - 2 * (cols - 1)) // cols
        for n, a in enumerate(active):
            agent_card(c, y, x0 + (n % cols) * (cw + 2), cw, a, now, frame)
            if n % cols == cols - 1 or n == len(active) - 1:
                y += 5
    if finished:
        section(c, y, x0, fw, "C O M P L E T E D", len(finished), GREEN)
        y += 2
        cols = max(1, min(3, fw // 60))
        cw = (fw - 3 * (cols - 1)) // cols
        for n, a in enumerate(finished):
            done_row(c, y, x0 + (n % cols) * (cw + 3), cw, a)
            if n % cols == cols - 1 or n == len(finished) - 1:
                y += 1
    if not session["agents"]:
        c.put(y, x0, "No sub-agents in this session yet.", FAINT)
        y += 1
    c.clip = (0, height)
    max_scroll = max(0, y + scroll - area_bottom)
    if scroll > 0:
        c.chip(area_top, x0 + fw - 14, "▲ more above", MUTED)
    if scroll < max_scroll:
        c.chip(area_bottom - 1, x0 + fw - 19, "▼ scroll for more", CYAN)

    feed(c, feed_top, x0, fw, session, now)
    footer(c)
    return c, session["id"], max_scroll


def sgr(fg, bg, bold):
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return f"\x1b[0;{'1;' if bold else ''}38;2;{f[0]};{f[1]};{f[2]};48;2;{b[0]};{b[1]};{b[2]}m"


def frame_text(canvas):
    """One ANSI frame with 24-bit color. Plain escape codes, so UTF-8 box and bar characters show correctly
    (macOS's system ncurses, which Homebrew Python's curses uses, breaks them)."""
    out = ["\x1b[?2026h\x1b[H"]  # synchronized update, cursor home
    for y, row in enumerate(canvas.cells):
        cells = row[:-1] if y == canvas.h - 1 else row  # keep the last cell empty so the screen never scrolls
        last = None
        for ch, fg, bg, bold in cells:
            if (fg, bg, bold) != last:
                out.append(sgr(fg, bg, bold))
                last = (fg, bg, bold)
            out.append(ch)
        out.append("\x1b[0m" + ("\r\n" if y < canvas.h - 1 else ""))
    out.append("\x1b[?2026l")
    return "".join(out)


def frame_html(canvas):
    rows = ['<div style="height:17px;white-space:pre">' + "".join(
        f'<span style="display:inline-block;height:17px;color:{fg};background:{bg};{"font-weight:700;" if bold else ""}">{html.escape(ch)}</span>'
        for ch, fg, bg, bold in row) + "</div>" for row in canvas.cells]
    return ('<!doctype html><meta charset="utf-8"><body style="margin:0;background:%s;font:13px/17px Menlo,monospace">%s'
            % (BG, "".join(rows)))


MOUSE = re.compile(r"\x1b\[<(\d+);\d+;\d+[Mm]")


def read_keys(fd, timeout):
    """Return a list of actions from stdin: (move, ±1), (scroll, ±N), (quit, 0)."""
    if not select.select([fd], [], [], timeout)[0]:
        return []
    data = os.read(fd, 4096).decode("utf-8", "ignore")
    actions = [("scroll", -3 if button == "64" else 3) for button in MOUSE.findall(data) if button in ("64", "65")]
    data = MOUSE.sub("", data)
    for seq, action in (("\x1b[A", ("move", -1)), ("\x1b[B", ("move", 1)), ("\x1b[5~", ("scroll", -10)), ("\x1b[6~", ("scroll", 10))):
        actions += [action] * data.count(seq)
        data = data.replace(seq, "")
    actions += [("move", -1)] * data.count("k") + [("move", 1)] * data.count("j")
    if "q" in data or "\x03" in data:
        actions.append(("quit", 0))
    return actions


def run():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    # Alternate screen, hidden cursor, SGR mouse reports (so the wheel is not turned into arrow keys).
    out.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")
    out.flush()
    tty.setcbreak(fd)
    selected, snapshot, fetched, frame, scroll = None, None, 0, 0, 0
    try:
        while True:
            if time.time() - fetched > 1.5:
                snapshot, fetched = state(), time.time()
            for action, amount in read_keys(fd, 0.08):
                if action == "quit":
                    return
                if action == "scroll":
                    scroll = max(0, scroll + amount)
                if action == "move" and snapshot["sessions"]:
                    ids = [s["id"] for s in snapshot["sessions"]]
                    index = ids.index(selected) if selected in ids else 0
                    selected, scroll = ids[(index + amount) % len(ids)], 0
            width, height = os.get_terminal_size(fd)
            if width < 100 or height < 26:
                out.write("\x1b[0m\x1b[H\x1b[2JMake the pane at least 100 x 26.")
            else:
                canvas, shown, max_scroll = draw(snapshot, selected, width, height, frame, scroll)
                selected = selected or shown
                if scroll > max_scroll:
                    scroll = max_scroll
                    canvas, _, _ = draw(snapshot, selected, width, height, frame, scroll)
                out.write(frame_text(canvas))
            out.flush()
            frame += 1
    finally:
        out.write("\x1b[0m\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        w, h = map(int, sys.argv[sys.argv.index("--snapshot") + 1].split("x"))
        frame = int(sys.argv[sys.argv.index("--frame") + 1]) if "--frame" in sys.argv else 0
        print(draw(state(), None, w, h, frame)[0].text())
    elif "--html" in sys.argv:
        i = sys.argv.index("--html")
        w, h = map(int, sys.argv[i + 1].split("x"))
        with open(sys.argv[i + 2], "w") as handle:
            handle.write(frame_html(draw(state(), None, w, h, 3)[0]))
    else:
        run()
