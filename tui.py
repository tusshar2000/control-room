#!/usr/bin/env python3
"""Control Room in the terminal: boxes for the lead and each phase, animated dotted lines for the flow.

Keys: up/down or j/k select a session, q quits.
Snapshot for checks: tui.py --snapshot 150x42 [--frame N]
"""
import curses
import locale
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import state  # noqa: E402  same data as the web view

ORDER = ["Research", "Plan", "Implement", "Review", "Fix", "Other"]
VERB = {"Read": "read", "Edit": "edit", "Write": "write", "Bash": "run", "Grep": "grep", "Glob": "glob", "Agent": "spawn",
        "Skill": "skill", "WebFetch": "fetch", "WebSearch": "search", "SubagentHandback": "hand back", "StructuredOutput": "report"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STATUS_STYLE = {"needs-you": "you", "check": "check", "working": "run", "running": "run", "done": "done", "stalled": "bad", "idle": "dim"}


def ago(sec):
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec / 3600:.1f}h".replace(".0h", "h")


def model_name(model):
    parts = (model or "").split("-")
    return f"{parts[1].capitalize()} {parts[2]}.{parts[3]}" if len(parts) >= 4 and parts[0] == "claude" else (model or "")


def model_style(model):
    for family, style in (("opus", "opus"), ("sonnet", "sonnet"), ("haiku", "haiku"), ("fable", "fable")):
        if family in (model or ""):
            return style
    return "dim"


class Canvas:
    """A grid of characters with one style per cell; curses or plain text draws it."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.cells = [[(" ", None) for _ in range(width)] for _ in range(height)]

    def put(self, y, x, text, style=None):
        if 0 <= y < self.h:
            for i, ch in enumerate(text):
                if 0 <= x + i < self.w:
                    self.cells[y][x + i] = (ch, style)

    def box(self, y, x, w, h, style, title="", title_style=None, right="", right_style=None):
        if w < 4 or h < 2:
            return
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", style)
        for row in range(1, h - 1):
            self.put(y + row, x, "│", style)
            self.put(y + row, x + w - 1, "│", style)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", style)
        if title:
            room = w - 6 - (len(right) + 3 if right else 0)
            self.put(y, x + 2, f" {title[:room]} ", title_style or style)
        if right:
            self.put(y, x + w - len(right) - 4, f" {right} ", right_style or style)

    def text(self):
        return "\n".join("".join(ch for ch, _ in row).rstrip() for row in self.cells)


def fit(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def connector(canvas, y, x, length, mode, frame, vertical=False):
    """Dotted line: 'flow' moves dots toward the target, 'passed' is solid, 'wait' is faint."""
    for i in range(length):
        if mode == "flow":
            ch, style = ("●", "run") if (i - frame) % 4 == 0 else ("·", "run")
        elif mode == "passed":
            ch, style = ("│" if vertical else "─"), "done"
        else:
            ch, style = ("┊" if vertical else "┄"), "dim"
        canvas.put(y + i if vertical else y, x if vertical else x + i, ch, style)
    if not vertical:
        canvas.put(y, x + length, "►", {"flow": "run", "passed": "done"}.get(mode, "dim"))


def lanes_for(session):
    phases = [p for p in ORDER if any(a["phase"] == p for a in session["agents"])]
    extra = [p for p in dict.fromkeys(a["phase"] for a in session["agents"]) if p not in ORDER]
    run = session["runs"][-1] if session["runs"] else None
    declared = run["phases"] if run and run["phases"] else []
    return list(dict.fromkeys(declared + phases + extra)), run


def narrate(session, lanes, run, now):
    """Two plain sentences: what happens now, and what happened in the last workflow."""
    running = [a for a in session["agents"] if a["status"] == "running"]
    if running:
        phase = running[0]["phase"].upper()
        steps = "; ".join(f"{fit(a['label'], 28)} → {VERB.get(a['activity']['tool'], a['activity']['tool'])} {a['activity']['target']}"
                          if a["activity"] else fit(a["label"], 28) for a in running)
        now_line = f"NOW: {phase} — {len(running)} agent{'s' if len(running) > 1 else ''} at work. {steps}"
    elif session["status"] == "working":
        last = session["feed"][-1] if session["feed"] else None
        now_line = "NOW: the lead works alone, no sub-agents." + (f" Last action: {VERB.get(last['tool'], last['tool'])} {last['target']}." if last else "")
    elif session["status"] == "needs-you":
        now_line = f"NOW: nothing runs. The lead waits for your reply ({ago(session['quiet'])})."
    elif session["status"] == "check":
        now_line = "NOW: a tool waits. Approve it in that pane, or a long command still runs."
    else:
        now_line = f"NOW: nothing runs. Last activity {ago(now - session['last'])} ago."
    if not run:
        return now_line, "No workflow in this session." + (f" {len(session['agents'])} sub-agents ran." if session["agents"] else "")
    counts = []
    for lane in lanes:
        items = [a for a in session["agents"] if a["phase"] == lane]
        done = sum(a["status"] == "done" for a in items)
        if not items:
            counts.append(f"{lane} skipped" if run["status"] == "done" else f"{lane} not started")
        else:
            counts.append(f"{lane} {done}/{len(items)}")
    state_word = "is running" if run["status"] == "running" else "finished"
    return now_line, f"WORKFLOW {run['name']} {state_word}: " + " → ".join(counts) + "."


def draw(snapshot, selected_id, width, height, frame):
    c = Canvas(width, height)
    now = snapshot["now"]
    sessions = snapshot["sessions"]
    if not sessions:
        c.put(1, 2, "No Claude Code sessions in the last 12 hours.", "dim")
        return c, None
    session = next((s for s in sessions if s["id"] == selected_id), sessions[0])
    agents = [a for s in sessions for a in s["agents"]]

    # Header
    c.put(0, 1, " CONTROL ROOM ", "title")
    anything_runs = any(a["status"] == "running" for a in agents) or any(s["status"] == "working" for s in sessions)
    c.put(0, 16, "●", ("run" if frame % 10 < 7 else "dim") if anything_runs else "done")
    c.put(0, 18, "live", "dim")
    stats = (f"{sum(s['status'] != 'idle' for s in sessions)} active   {sum(a['status'] == 'running' for a in agents)} running   "
             f"{sum(a['status'] == 'done' for a in agents)} done   {sum(a['calls'] for a in agents)} tool calls   {time.strftime('%H:%M')}")
    c.put(0, width - len(stats) - 2, stats, "dim")

    # Look here next
    top = 2
    focus = next((s for s in sessions if s["status"] == "needs-you"), None) or next((s for s in sessions if s["status"] == "check"), None)
    if focus:
        style = "you" if focus["status"] == "needs-you" else "check"
        label = "▶ LOOK HERE NEXT" if style == "you" else "▶ CHECK THIS PANE"
        where = f"{focus['repo']} · {focus['branch'] or ''}"
        wait = f"waiting {ago(focus['quiet'])}"
        c.box(1, 0, width, 3, style)
        c.put(2, 2, label, style + "-bold")
        c.put(2, 21, where, "bold")
        msg = "a tool waits: approve it, or a long command runs" if style == "check" else (focus["lastText"].splitlines() or [""])[0]
        c.put(2, 23 + len(where), fit(msg, width - len(where) - len(wait) - 28), "dim")
        c.put(2, width - len(wait) - 3, wait, style)
        top = 5

    # Sessions list
    side = 30 if width >= 160 else 24 if width >= 120 else 0
    if side:
        c.box(top, 0, side, height - top - 1, "line", "SESSIONS", "dim", str(len(sessions)), "dim")
        y = top + 1
        for s in sessions:
            if y + 2 > height - 2:
                break
            mark = "▌" if s["id"] == session["id"] else " "
            c.put(y, 1, mark, STATUS_STYLE[s["status"]])
            c.put(y, 3, "●", STATUS_STYLE[s["status"]])
            c.put(y, 5, fit(s["repo"], side - 13), "bold" if s["id"] == session["id"] else None)
            c.put(y, side - 7, ago(now - s["last"]).rjust(5), "dim")
            c.put(y + 1, 5, fit(s["branch"] or "no branch", side - 7), "dim")
            y += 3

    # Flow area
    x0 = side + 1 if side else 0
    fw = width - x0
    lanes, run = lanes_for(session)
    head = f"{session['repo']}  {session['branch'] or ''}"
    c.put(top, x0 + 1, head, "bold")
    status_text = session["status"].replace("-", " ").upper()
    c.put(top, x0 + len(head) + 3, f" {status_text} ", STATUS_STYLE[session["status"]] + "-inv")
    if run:
        tag = f"workflow {run['name']} · {run['agents']} agents · {run['status']}"
        c.put(top, x0 + fw - len(tag) - 2, tag, "run" if run["status"] == "running" else "dim")

    columns = ["LEAD"] + lanes
    gap = 6
    while True:
        bw = min(32, (fw - 2 - gap * (len(columns) - 1)) // len(columns))
        if bw >= 17 or len(columns) <= 2:
            break
        columns.pop()  # ponytail: very narrow panes drop the last lanes; widen the pane to see them
    now_line, flow_line = narrate(session, lanes, run, now)
    c.box(top + 1, x0 + 1, fw - 2, 4, "line", "WHAT IS HAPPENING", "dim")
    c.put(top + 2, x0 + 3, fit(now_line, fw - 6), "you" if session["status"] == "needs-you" else "run" if now_line.startswith("NOW: ") and "at work" in now_line else "bold")
    c.put(top + 3, x0 + 3, fit(flow_line, fw - 6))
    y_head = top + 6
    agents_by_lane = {p: [a for a in session["agents"] if a["phase"] == p] for p in lanes}

    def lane_state(name):
        if name == "LEAD":
            return "running" if session["status"] == "working" else "done"
        items = agents_by_lane[name]
        if any(a["status"] == "running" for a in items):
            return "running"
        return "done" if items else "wait"

    for i, name in enumerate(columns):
        x = x0 + 1 + i * (bw + gap)
        st = lane_state(name)
        border = {"running": "run", "done": "done", "wait": "dim"}[st]
        if name == "LEAD":
            icon = SPIN[frame % len(SPIN)] if session["status"] == "working" else ("◆" if session["status"] == "needs-you" else "○")
            c.box(y_head, x, bw, 4, STATUS_STYLE[session["status"]], "LEAD", "bold")
            c.put(y_head + 1, x + 2, f"{icon} {fit(model_name(session['model']), bw - 6)}", model_style(session["model"]))
            c.put(y_head + 2, x + 2, fit(f"ctx {session['context'] // 1000}k · {status_text.lower()}", bw - 4), "dim")
        else:
            items = agents_by_lane[name]
            done = sum(a["status"] == "done" for a in items)
            icon = SPIN[frame % len(SPIN)] if st == "running" else ("✓" if st == "done" else "○")
            c.box(y_head, x, bw, 4, border, name.upper(), "bold")
            c.put(y_head + 1, x + 2, icon, border)
            bar_w = bw - 6
            filled = round(bar_w * done / len(items)) if items else 0
            c.put(y_head + 1, x + 4, "█" * filled + "░" * (bar_w - filled), border)
            c.put(y_head + 2, x + 2, fit("skipped" if not items and run and run["status"] == "done" else
                                         f"{sum(a['status'] == 'running' for a in items)} running · {done}/{len(items)}" if st == "running" else
                                         f"done · {done}/{len(items)}" if st == "done" else "not started", bw - 4), "dim")

            # Agents stacked under the phase, joined by a vertical dotted line
            y = y_head + 4
            room = height - y - 9
            shown = items[: max(0, room // 6)]
            for a in shown:
                connector(c, y, x + bw // 2, 1, "flow" if a["status"] == "running" else "passed", frame, vertical=True)
                y += 1
                astyle = STATUS_STYLE[a["status"]]
                aicon = SPIN[frame % len(SPIN)] if a["status"] == "running" else "✓" if a["status"] == "done" else "!"
                c.box(y, x, bw, 5, astyle if a["status"] != "done" else "line", a["type"], "role-" + a["type"] if a["type"] in ("planner", "implementer", "reviewer") else "dim", aicon, astyle)
                c.put(y + 1, x + 2, fit(a["label"], bw - 4), "bold")
                act = a["activity"]
                line = (f"finished {ago(now - a['last'])} ago" if a["status"] == "done"
                        else f"▸ {VERB.get(act['tool'], act['tool'])} {act['target']}" if act else "▸ starting")
                c.put(y + 2, x + 2, fit(line, bw - 4), "run" if a["status"] == "running" else "dim")
                end = a["last"] if a["status"] == "done" else now
                meta = fit(f"{ago(end - a['start'])} · {a['calls']} call{'' if a['calls'] == 1 else 's'}", bw - 4)
                if bw - len(meta) - 5 >= 6:
                    c.put(y + 3, x + 2, fit(model_name(a["model"]), bw - len(meta) - 5), model_style(a["model"]))
                c.put(y + 3, x + bw - len(meta) - 2, meta, "dim")
                y += 5
            if len(items) > len(shown):
                c.put(y, x + 2, f"+{len(items) - len(shown)} more", "dim")

        if i:
            prev = columns[i - 1]
            target = lane_state(name)
            mode = "flow" if target == "running" else "passed" if target == "done" and lane_state(prev) != "wait" else "wait"
            connector(c, y_head + 1, x - gap + 1, gap - 3, mode, frame)

    # Lead message and activity feed at the bottom
    feed_top = height - 8
    c.put(feed_top, x0 + 1, "─" * (fw - 2), "line")
    c.put(feed_top, x0 + 3, " LEAD SAYS ", "dim")
    c.put(feed_top + 1, x0 + 2, fit(session["lastText"], fw - 4))
    c.put(feed_top + 2, x0 + 3, " ACTIVITY ", "dim")
    for row, e in enumerate(reversed(session["feed"][-4:])):
        y = feed_top + 3 + row
        who = e["who"]
        c.put(y, x0 + 2, ago(now - e["t"]).rjust(4), "dim")
        c.put(y, x0 + 8, who[:11], "role-" + who if who in ("planner", "implementer", "reviewer") else "run" if who == "lead" else "dim")
        c.put(y, x0 + 21, VERB.get(e["tool"], e["tool"] or ""), "bold")
        c.put(y, x0 + 32, fit(e["target"], fw - 34), "dim")
    c.put(height - 1, 1, "↑/↓ session   q quit     ", "dim")
    legend_x = 27
    for text, style in (("●··►", "run"), (" data moves now   ", "dim"), ("───►", "done"), (" work passed   ", "dim"), ("┄┄┄►", "dim"), (" not started", "dim")):
        c.put(height - 1, legend_x, text, style)
        legend_x += len(text)
    return c, session["id"]


def styles():
    curses.start_color()
    curses.use_default_colors()
    palette = {"run": curses.COLOR_CYAN, "you": curses.COLOR_YELLOW, "check": curses.COLOR_YELLOW, "done": curses.COLOR_GREEN,
               "bad": curses.COLOR_RED, "opus": curses.COLOR_MAGENTA, "sonnet": curses.COLOR_BLUE, "haiku": curses.COLOR_GREEN,
               "fable": curses.COLOR_RED, "dim": 8 if curses.COLORS > 8 else curses.COLOR_WHITE, "line": 8 if curses.COLORS > 8 else curses.COLOR_WHITE,
               "role-planner": curses.COLOR_MAGENTA, "role-implementer": curses.COLOR_BLUE, "role-reviewer": curses.COLOR_YELLOW}
    attrs = {"bold": curses.A_BOLD, "title": curses.A_REVERSE | curses.A_BOLD, None: 0}
    for n, (name, color) in enumerate(palette.items(), start=1):
        curses.init_pair(n, color, -1)
        attrs[name] = curses.color_pair(n) | (curses.A_DIM if name in ("dim", "line") and curses.COLORS <= 8 else 0)
        attrs[name + "-bold"] = attrs[name] | curses.A_BOLD
        attrs[name + "-inv"] = attrs[name] | curses.A_REVERSE | curses.A_BOLD
    return attrs


def run(screen):
    curses.curs_set(0)
    screen.nodelay(True)
    attrs = styles()
    selected, snapshot, fetched, frame = None, None, 0, 0
    while True:
        if time.time() - fetched > 1.5:
            snapshot, fetched = state(), time.time()
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k")) and snapshot["sessions"]:
            ids = [s["id"] for s in snapshot["sessions"]]
            index = ids.index(selected) if selected in ids else 0
            selected = ids[(index + (1 if key in (curses.KEY_DOWN, ord("j")) else -1)) % len(ids)]
        height, width = screen.getmaxyx()
        screen.erase()
        if width < 90 or height < 24:
            screen.addstr(0, 0, "Make the pane at least 90 x 24."[: width - 1])
        else:
            canvas, shown = draw(snapshot, selected, width, height, frame)
            selected = selected or shown
            for y, row in enumerate(canvas.cells[: height]):
                x = 0
                while x < width:
                    style = row[x][1]
                    end = x
                    while end < width and row[end][1] == style:
                        end += 1
                    chunk = "".join(ch for ch, _ in row[x:end])
                    if y == height - 1 and end == width:
                        chunk = chunk[:-1]  # curses cannot write the bottom-right cell
                    try:
                        screen.addstr(y, x, chunk, attrs.get(style, 0))
                    except curses.error:
                        pass
                    x = end
        screen.refresh()
        frame += 1
        time.sleep(0.12)


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        w, h = map(int, sys.argv[sys.argv.index("--snapshot") + 1].split("x"))
        frame = int(sys.argv[sys.argv.index("--frame") + 1]) if "--frame" in sys.argv else 0
        print(draw(state(), None, w, h, frame)[0].text())
    else:
        locale.setlocale(locale.LC_ALL, "")
        curses.wrapper(run)
