import argparse
import os
import sys
import time
import contextlib
import json
import re
import shutil
import threading
import unicodedata
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

BASE_URL = "https://jules.googleapis.com/v1alpha"

# ANSI color codes
COLOR_USER = "\033[92m"    # Green
COLOR_JULES = "\033[94m"   # Blue
COLOR_SYSTEM = "\033[93m"  # Yellow
COLOR_PLAN = "\033[95m"    # Magenta
COLOR_DIM = "\033[90m"     # Grey
COLOR_CODE = "\033[96m"    # Cyan, for `inline code`
COLOR_ERROR = "\033[91m"   # Red
COLOR_RESET = "\033[0m"

# Attribute toggles. Turning an attribute off leaves the surrounding colour intact.
BOLD, BOLD_OFF = "\033[1m", "\033[22m"
ITALIC, ITALIC_OFF = "\033[3m", "\033[23m"

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# Session states in which the agent is working and we should keep polling.
BUSY_STATES = {"QUEUED", "PLANNING", "IN_PROGRESS"}
# Session states in which the ball is back in the user's court.
WAITING_STATES = {"AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK", "PAUSED"}
TERMINAL_STATES = {"COMPLETED", "FAILED"}
# How long to keep waiting for Jules to pick up newly sent work before trusting
# an idle session state. The API keeps reporting the pre-sendMessage state for a
# few seconds, so a zero window would end the wait before Jules even starts.
# Measured against the live API the state flips within ~6s, so this is roughly a
# 7x margin. Keep it small: it is the worst-case stall when Jules decides a
# message needs no reply at all, and until it expires the prompt is blocked.
STARTUP_GRACE = 45

# Every activity payload the API can attach to an activity.
ACTIVITY_KINDS = (
    "userMessaged",
    "agentMessaged",
    "planGenerated",
    "planApproved",
    "progressUpdated",
    "sessionCompleted",
    "sessionFailed",
)


def setup_console():
    """Enable VT100 escapes and UTF-8 I/O so non-ASCII replies do not crash us."""
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            for handle in (-11, -12):  # stdout, stderr
                mode = ctypes.c_ulong()
                h = kernel32.GetStdHandle(handle)
                if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(h, mode.value | 0x0004)
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Importing readline is what gives input() arrow-key editing and history on
    # POSIX. Without it an arrow key is delivered raw, as "\033[A" inside the
    # line. Not available on Windows, whose console does its own line editing.
    have_readline = True
    try:
        import readline  # noqa: F401
    except Exception:
        have_readline = False

    global _interactive, _can_redraw
    try:
        _interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        _interactive = False
    # Redrawing the prompt underneath streamed output needs readline to tell us
    # what has been typed so far. Windows input() keeps that to itself, so there
    # we queue output rather than scribble over the input line.
    _can_redraw = _interactive and have_readline and os.name != "nt"


def flush_input():
    """Drop keystrokes typed while we were busy polling.

    Anything typed during a wait sits in the terminal buffer and would be read
    by the next input() as if it were the reply -- arrow keys included, which
    arrive as escape sequences rather than moving the cursor.
    """
    try:
        if not sys.stdin.isatty():
            return
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def clean_input(text):
    """Strip escape sequences and stray control characters from typed input.

    Belt and braces for whatever survives flush_input(): we must never forward
    a raw "\033[A" to Jules as if the user had typed it.
    """
    text = ANSI_RE.sub("", text)
    # Any other CSI/escape sequence, then leftover control characters.
    text = re.sub(r"\033\[[0-9;?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\033.", "", text)
    return "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32).strip()


def load_api_key():
    load_dotenv()
    api_key = os.environ.get("JULES_API_KEY")
    if not api_key:
        print("Error: JULES_API_KEY not found in .env file or environment variables.")
        sys.exit(1)
    return api_key


def get_headers(api_key):
    return {"X-Goog-Api-Key": api_key, "Content-Type": "application/json"}


def term_width():
    return max(40, min(shutil.get_terminal_size((100, 24)).columns, 110))


def parse_ts(value):
    """Parse an RFC-3339 timestamp with any fractional-second precision."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        text = value.rstrip("Z")
        if "." in text:
            base, frac = text.split(".", 1)
            text = "{}.{}".format(base, (frac + "000000")[:6])
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def display_width(text):
    """Columns the text occupies. CJK and emoji take two, escapes take none."""
    width = 0
    for char in strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def split_chunks(line):
    """Break a line into (chunk, preceded_by_space) pairs.

    Wide characters become their own chunk because CJK text has no spaces to
    break on; ANSI escapes stay glued to the word they style.
    """
    chunks = []
    word = ""
    spaced = False
    for char in line:
        if char.isspace():
            if word:
                chunks.append((word, spaced))
                word = ""
            spaced = True
        elif unicodedata.east_asian_width(char) in ("W", "F"):
            if word:
                chunks.append((word, spaced))
                word = ""
                spaced = False
            chunks.append((char, spaced))
            spaced = False
        else:
            word += char
    if word:
        chunks.append((word, spaced))
    return chunks


def wrap(text, indent="", hanging=None):
    """Wrap to the terminal by display width, honouring a hanging indent."""
    hanging = indent if hanging is None else hanging
    out = []
    for raw in str(text).replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            out.append("")
            continue
        leading = re.match(r"[ \t]*", raw).group(0)
        first, rest = indent + leading, hanging + leading
        prefix = first
        limit = max(20, term_width() - display_width(prefix))
        line = ""
        for chunk, spaced in split_chunks(raw):
            sep = " " if spaced and line else ""
            if line and display_width(line + sep + chunk) > limit:
                out.append(prefix + line)
                prefix = rest
                limit = max(20, term_width() - display_width(prefix))
                line = chunk
            else:
                line += sep + chunk
        out.append(prefix + line)
    return "\n".join(out)


def md_inline(text, base=""):
    """Render inline markdown as ANSI, returning to `base` after each span.

    Anything that emits an escape sequence is parked in `spans` first, because
    an escape contains a "[" that later bracket-matching patterns would eat.
    """
    spans = []

    def stash(rendered):
        spans.append(rendered)
        return "\x00{}\x00".format(len(spans) - 1)

    # Code first: markdown inside a code span is literal.
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: stash(COLOR_CODE + m.group(1) + base),
        text,
    )
    # Links next, while the brackets in the text are still the only brackets.
    text = re.sub(
        r"\[([^\]\n]*)\]\(([^)\s]+)\)",
        lambda m: m.group(1) + stash("{} ({}){}".format(COLOR_DIM, m.group(2), base)),
        text,
    )
    text = re.sub(
        r"\*\*(?!\s)(.+?)(?<!\s)\*\*",
        lambda m: stash(BOLD) + m.group(1) + stash(BOLD_OFF),
        text,
        flags=re.S,
    )
    text = re.sub(
        r"(?<![\w_])__(?!\s)(.+?)(?<!\s)__(?![\w_])",
        lambda m: stash(BOLD) + m.group(1) + stash(BOLD_OFF),
        text,
        flags=re.S,
    )
    text = re.sub(
        r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])",
        lambda m: stash(ITALIC) + m.group(1) + stash(ITALIC_OFF),
        text,
    )
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


# ---------------------------------------------------------------------------
# Output layer.
#
# A background thread streams Jules' activity while the main thread sits in
# input(). Printing from the poller straight to stdout would land in the middle
# of whatever the user is typing, so every write goes through emit(), which
# erases the input line, prints, and puts the prompt and the half-typed text
# back underneath.
#
# We deliberately keep input() rather than reading keystrokes ourselves: it is
# what makes IME and multi-byte (e.g. Chinese) input work. The cost is that only
# readline can tell us the half-typed text, so the redraw is POSIX-only. Where
# it is unavailable, output is queued until the user presses Enter instead --
# delayed, but never corrupted.
# ---------------------------------------------------------------------------

_out_lock = threading.RLock()
_interactive = False   # stdin and stdout are both a terminal
_can_redraw = False    # ...and readline can hand us the half-typed line
_at_prompt = False
_prompt_text = ""
_pending = []


def _line_buffer():
    try:
        import readline

        return readline.get_line_buffer()
    except Exception:
        return ""


def emit(text):
    """Print without disturbing whatever the user is typing."""
    with _out_lock:
        clear_status()
        if _at_prompt and not _can_redraw:
            # Cannot restore the input line, so hold it back rather than
            # scribble over what is being typed.
            _pending.append(text)
            return
        if _at_prompt:
            sys.stdout.write("\r\033[K")
        print(text)
        if _at_prompt:
            sys.stdout.write(_prompt_text + _line_buffer())
            sys.stdout.flush()


def set_prompt(text, active):
    """Mark the input line as on screen (and what it looks like)."""
    global _at_prompt, _prompt_text
    with _out_lock:
        _prompt_text = text
        _at_prompt = bool(active and _interactive)
        if not _at_prompt:
            _drain_pending()


def _drain_pending():
    global _pending
    with _out_lock:
        held, _pending = _pending, []
        for text in held:
            print(text)


def say(color, text):
    emit("{}{}{}".format(color, text, COLOR_RESET))


def say_markdown(text, base, indent="  "):
    """Render a markdown block the way the Jules web UI shows it."""
    in_fence = False
    for raw in str(text).replace("\r\n", "\n").split("\n"):
        fence = re.match(r"\s*```(.*)$", raw)
        if fence:
            in_fence = not in_fence
            label = fence.group(1).strip()
            say(COLOR_DIM, indent + ("--- " + label if in_fence and label else "---"))
            continue
        if in_fence:
            say(COLOR_CODE, indent + "  " + raw)
            continue
        if not raw.strip():
            emit("")
            continue
        if re.match(r"\s*([-*_])(\s*\1){2,}\s*$", raw):
            say(COLOR_DIM, indent + "-" * max(0, term_width() - display_width(indent)))
            continue

        heading = re.match(r"\s*(#{1,6})\s+(.*)$", raw)
        if heading:
            say(base, wrap(BOLD + md_inline(heading.group(2), base) + BOLD_OFF, indent))
            continue

        quote = re.match(r"\s*>\s?(.*)$", raw)
        if quote:
            say(COLOR_DIM, wrap(md_inline(quote.group(1)), indent + "| "))
            continue

        numbered = re.match(r"(\s*)(\d+)[.)]\s+(.*)$", raw)
        if numbered:
            lead, marker, body = numbered.groups()
            first = "{}{}{}. ".format(indent, lead, marker)
            say(base, wrap(md_inline(body, base), first, " " * display_width(first)))
            continue

        bullet = re.match(r"(\s*)[-*+]\s+(.*)$", raw)
        if bullet:
            lead, body = bullet.groups()
            first = "{}{}- ".format(indent, lead)
            say(base, wrap(md_inline(body, base), first, " " * display_width(first)))
            continue

        say(base, wrap(md_inline(raw, base), indent))


# True while a transient status line is on screen with the cursor parked on it.
# Anything that prints must clear it first, or it will overwrite only part of
# the status text and leave fragments behind mid-line.
_status_shown = False


def status(text):
    """Write a transient one-line status that the next status() overwrites."""
    global _status_shown
    # Keep it inside one terminal row; a wrapped status cannot be erased by \r.
    room = term_width() - 1
    if display_width(text) > room:
        text = text[:room]
    sys.stdout.write("\r{}{}{}\033[K".format(COLOR_DIM, text, COLOR_RESET))
    sys.stdout.flush()
    _status_shown = True


def clear_status():
    global _status_shown
    if not _status_shown:
        return
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    _status_shown = False


@contextlib.contextmanager
def muted_input():
    """Stop the terminal echoing keystrokes typed while we are polling.

    Without this, anything typed during a wait is echoed wherever the cursor
    happens to be, splicing the user's words into the middle of Jules' output.
    The keystrokes are discarded by flush_input() before the next prompt.
    """
    restore = None
    try:
        if sys.stdin.isatty():
            if os.name == "nt":
                import ctypes

                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
                mode = ctypes.c_ulong()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    saved = mode.value
                    kernel32.SetConsoleMode(handle, saved & ~0x0004)  # ENABLE_ECHO_INPUT
                    restore = lambda: kernel32.SetConsoleMode(handle, saved)
            else:
                import termios

                fd = sys.stdin.fileno()
                saved = termios.tcgetattr(fd)
                muted = termios.tcgetattr(fd)
                muted[3] &= ~termios.ECHO  # lflags
                termios.tcsetattr(fd, termios.TCSADRAIN, muted)
                restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except Exception:
        restore = None
    try:
        yield
    finally:
        if restore:
            try:
                restore()
            except Exception:
                pass


class JulesError(Exception):
    pass


class JulesClient:
    def __init__(self, headers):
        self.headers = headers

    def _url(self, session_name, suffix=""):
        name = session_name if session_name.startswith("sessions/") else "sessions/" + session_name
        return "{}/{}{}".format(BASE_URL, name, suffix)

    def _request(self, method, url, **kwargs):
        try:
            response = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise JulesError("Network error: {}".format(exc))
        if response.status_code >= 400:
            detail = response.text.strip()
            try:
                detail = response.json()["error"]["message"]
            except Exception:
                pass
            raise JulesError("HTTP {}: {}".format(response.status_code, detail))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def list_sessions(self, page_size=20):
        data = self._request("GET", "{}/sessions".format(BASE_URL), params={"pageSize": page_size})
        if isinstance(data, list):
            return data
        return data.get("sessions", [])

    def get_session(self, session_name):
        return self._request("GET", self._url(session_name))

    def list_activities(self, session_name):
        """Fetch every activity, de-duplicated and sorted oldest-first."""
        url = self._url(session_name, "/activities")
        activities = []
        seen = set()
        page_token = None
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", url, params=params)
            page = data if isinstance(data, list) else data.get("activities", [])
            for activity in page:
                key = activity_key(activity)
                if key in seen:
                    continue
                seen.add(key)
                activities.append(activity)
            page_token = None if isinstance(data, list) else data.get("nextPageToken")
            if not page_token:
                break
        return sort_activities(activities)

    def send_message(self, session_name, prompt):
        self._request("POST", self._url(session_name, ":sendMessage"), json={"prompt": prompt})

    def approve_plan(self, session_name):
        self._request("POST", self._url(session_name, ":approvePlan"), json={})


def activity_key(activity):
    return activity.get("name") or activity.get("id") or json.dumps(activity, sort_keys=True)


def sort_activities(activities):
    """Oldest first. The API does not guarantee order, so never rely on it."""
    ordered = sorted(
        enumerate(activities),
        key=lambda pair: (parse_ts(pair[1].get("createTime")), pair[0]),
    )
    return [activity for _, activity in ordered]


def activity_kind(activity):
    for kind in ACTIVITY_KINDS:
        if kind in activity:
            return kind
    return None


def diff_stats(patch):
    files = len(re.findall(r"^diff --git ", patch, re.MULTILINE))
    added = len(re.findall(r"^\+(?!\+\+)", patch, re.MULTILINE))
    removed = len(re.findall(r"^-(?!--)", patch, re.MULTILINE))
    return files, added, removed


class ChatSession:
    """Append-only renderer and interactive loop for one Jules session."""

    def __init__(self, client, session_name):
        self.client = client
        self.session_name = session_name
        self.seen = set()
        self.verbose = False
        self.last_plan = None
        self.last_change_set = None
        self.state = "UNKNOWN"
        self.url = ""
        self.stop = threading.Event()
        # One API conversation at a time: the poller thread and the prompt
        # thread both sync, and self.seen must not be raced.
        self.api_lock = threading.RLock()
        self.sent_at = 0.0

    # ---------- rendering ----------

    def render(self, activity):
        kind = activity_kind(activity)
        payload = activity.get(kind, {}) if kind else {}

        if kind == "userMessaged":
            say(COLOR_USER, "You:")
            say_markdown(payload.get("userMessage", ""), COLOR_USER)
            emit("")
        elif kind == "agentMessaged":
            say(COLOR_JULES, "Jules:")
            say_markdown(payload.get("agentMessage", ""), COLOR_JULES)
            emit("")
        elif kind == "planGenerated":
            self.render_plan(payload.get("plan", {}))
        elif kind == "planApproved":
            say(COLOR_SYSTEM, "[plan approved]\n")
        elif kind == "progressUpdated":
            title = payload.get("title", "").strip()
            description = payload.get("description", "").strip()
            say(COLOR_DIM, wrap("- {}".format(title or "working...")))
            if description and self.verbose:
                say(COLOR_DIM, wrap(description, "    "))
        elif kind == "sessionCompleted":
            say(COLOR_SYSTEM, "\n[session completed]\n")
        elif kind == "sessionFailed":
            reason = payload.get("reason", "no reason given")
            say(COLOR_ERROR, "\n[session failed] {}\n".format(reason))
        elif kind is None:
            # Unknown or newly added activity type: show it rather than dropping it.
            body = {
                k: v
                for k, v in activity.items()
                if k not in ("name", "id", "createTime", "originator", "artifacts")
            }
            if body:
                say(
                    COLOR_DIM,
                    wrap(
                        "[{}] {}".format(
                            activity.get("originator", "system"),
                            json.dumps(body, ensure_ascii=False),
                        ),
                        "  ",
                    ),
                )

        for artifact in activity.get("artifacts", []) or []:
            self.render_artifact(artifact)

    def render_plan(self, plan):
        self.last_plan = plan
        steps = sorted(plan.get("steps", []) or [], key=lambda s: s.get("index", 0))
        heading = "-- Plan ({} step{}) ".format(len(steps), "" if len(steps) == 1 else "s")
        width = term_width()
        say(COLOR_PLAN, heading + "-" * max(0, width - display_width(heading)))
        for number, step in enumerate(steps, start=1):
            first = "  {}. ".format(number)
            say(COLOR_PLAN, wrap(
                md_inline(step.get("title", "(untitled)"), COLOR_PLAN),
                first,
                " " * display_width(first),
            ))
            description = (step.get("description") or "").strip()
            if description:
                say_markdown(description, COLOR_DIM, indent=" " * display_width(first))
        say(COLOR_PLAN, "-" * width)
        emit("")

    def render_artifact(self, artifact):
        if "changeSet" in artifact:
            change_set = artifact["changeSet"]
            self.last_change_set = change_set
            patch = (change_set.get("gitPatch") or {}).get("unidiffPatch", "")
            files, added, removed = diff_stats(patch)
            message = (change_set.get("gitPatch") or {}).get("suggestedCommitMessage", "")
            headline = message.splitlines()[0] if message.strip() else ""
            say(COLOR_SYSTEM, "[changes] {} file(s), +{} -{}{}".format(
                files, added, removed, '  "{}"'.format(headline) if headline else ""
            ))
            if patch:
                if self.verbose:
                    say(COLOR_DIM, patch)
                else:
                    say(COLOR_DIM, "  (/diff to view the patch)")
            emit("")
        elif "bashOutput" in artifact:
            bash = artifact["bashOutput"]
            say(COLOR_DIM, "  $ {}".format(bash.get("command", "")))
            lines = (bash.get("output") or "").splitlines()
            shown = lines if self.verbose else lines[:12]
            for line in shown:
                say(COLOR_DIM, "  | {}".format(line))
            if len(lines) > len(shown):
                say(COLOR_DIM, "  | ... {} more line(s), /verbose to show".format(
                    len(lines) - len(shown)
                ))
            if bash.get("exitCode"):
                say(COLOR_ERROR, "  | exit code {}".format(bash["exitCode"]))
        elif "media" in artifact:
            media = artifact["media"]
            say(COLOR_DIM, "  [media {} ({} bytes)]".format(
                media.get("mimeType", "?"), len(media.get("data") or "")
            ))

    # ---------- syncing ----------

    def sync(self, quiet=False):
        """Print every activity not printed yet. Returns the new ones."""
        with self.api_lock:
            try:
                activities = self.client.list_activities(self.session_name)
            except JulesError as exc:
                if not quiet:
                    say(COLOR_ERROR, "Could not fetch activities: {}".format(exc))
                return []

            fresh = []
            for activity in sort_activities(activities):
                key = activity_key(activity)
                if key in self.seen:
                    continue
                self.seen.add(key)
                self.render(activity)
                fresh.append(activity)
            return fresh

    def refresh_state(self, quiet=True):
        with self.api_lock:
            return self._refresh_state(quiet)

    def _refresh_state(self, quiet=True):
        try:
            session = self.client.get_session(self.session_name)
        except JulesError as exc:
            if not quiet:
                say(COLOR_ERROR, "Could not fetch session: {}".format(exc))
            return self.state
        self.state = session.get("state", "UNKNOWN")
        self.url = session.get("url", "")
        return self.state

    def wait_for_idle(self, timeout=900, interval=2):
        """Block until Jules stops working.

        Only watches state; the poller thread is what prints. Useful when you
        would rather not type over streaming output.
        """
        spinner = "|/-\\"
        started = time.time()
        tick = 0
        idle_polls = 0
        try:
            with muted_input():
                while time.time() - started < timeout:
                    state = self.state
                    # Straight after sending, the state still predates the
                    # message; see STARTUP_GRACE.
                    starting = (time.time() - self.sent_at) < STARTUP_GRACE
                    idle_polls = 0 if state in BUSY_STATES else idle_polls + 1
                    if idle_polls >= 2 and not starting:
                        clear_status()
                        return state
                    label = "waiting for jules" if starting else state.replace("_", " ").lower()
                    status(" {} {}  (ctrl-c to stop waiting) ".format(
                        spinner[tick % len(spinner)], label))
                    tick += 1
                    time.sleep(interval)
        except KeyboardInterrupt:
            clear_status()
            say(COLOR_SYSTEM, "(stopped waiting - Jules keeps working)")
            return self.state
        clear_status()
        say(COLOR_SYSTEM, "(gave up waiting after {}s)".format(timeout))
        return self.state

    # ---------- commands ----------

    def show_help(self):
        say(COLOR_SYSTEM, """Commands:
  /approve, /a   approve the pending plan (or just type "y" when one is pending)
  /plan          re-print the most recent plan
  /diff          print the most recent change set patch
  /state         show session state and web URL
  /wait, /w      block until Jules stops working (replies stream in anyway)
  /refresh, /r   fetch and print anything new right now
  /verbose, /v   toggle full progress details, bash output and diffs
  /clear         clear the screen
  /help, /?      show this help
  /exit, /quit   leave (the Jules session keeps running)
Anything else is sent to Jules as a message. Jules' replies stream in on
their own -- you can keep typing while it works.""")

    def approval_banner(self):
        """The CLI equivalent of the web UI's "Approve plan?" button."""
        say(COLOR_PLAN, "{}Approve plan?{}  y = approve   /plan = re-read   "
                        "or type feedback to revise it".format(BOLD, BOLD_OFF))

    def approve(self):
        if self.state != "AWAITING_PLAN_APPROVAL":
            say(COLOR_SYSTEM, "No plan is awaiting approval (state: {}).".format(self.state))
            return
        try:
            self.client.approve_plan(self.session_name)
        except JulesError as exc:
            say(COLOR_ERROR, "Approve failed: {}".format(exc))
            return
        say(COLOR_SYSTEM, "Plan approved.")
        self.sent_at = time.time()

    def show_plan(self):
        if not self.last_plan:
            say(COLOR_SYSTEM, "No plan has been generated in this session yet.")
            return
        self.render_plan(self.last_plan)

    def show_diff(self):
        patch = ((self.last_change_set or {}).get("gitPatch") or {}).get("unidiffPatch", "")
        if not patch:
            say(COLOR_SYSTEM, "No change set has been produced in this session yet.")
            return
        emit(patch)

    def show_state(self):
        state = self.refresh_state(quiet=False)
        say(COLOR_SYSTEM, "State: {}".format(state))
        if self.url:
            say(COLOR_SYSTEM, "URL:   {}".format(self.url))

    # ---------- main loop ----------

    def prompt_label(self):
        if self.state == "AWAITING_PLAN_APPROVAL":
            return "{}[plan awaiting approval - /approve or y]{}\n{}> {}".format(
                COLOR_PLAN, COLOR_RESET, COLOR_USER, COLOR_RESET
            )
        if self.state in TERMINAL_STATES:
            return "{}[{}]{} {}> {}".format(
                COLOR_DIM, self.state.lower(), COLOR_RESET, COLOR_USER, COLOR_RESET
            )
        return "{}> {}".format(COLOR_USER, COLOR_RESET)

    def poll_once(self):
        """One poll cycle: stream anything new, then announce state changes."""
        previous = self.state
        self.sync(quiet=True)
        state = self.refresh_state()
        if state == previous:
            return state

        if state == "AWAITING_PLAN_APPROVAL":
            self.approval_banner()
        elif state == "FAILED":
            say(COLOR_ERROR, "[session failed]")
        elif previous in BUSY_STATES and state not in BUSY_STATES:
            say(COLOR_DIM, "  (jules is idle)")
        elif state in BUSY_STATES and previous not in BUSY_STATES:
            say(COLOR_DIM, "  (jules is working)")
        return state

    def poll_loop(self, interval=3):
        """Background streaming. Errors must never take the prompt down."""
        while not self.stop.wait(interval):
            try:
                self.poll_once()
            except Exception:
                pass

    def run(self):
        say(COLOR_SYSTEM, "--- {} ---".format(self.session_name))
        say(COLOR_DIM, "/help for commands\n")

        self.sync()
        self.refresh_state(quiet=False)
        if self.state == "AWAITING_PLAN_APPROVAL":
            self.approval_banner()

        poller = threading.Thread(target=self.poll_loop, daemon=True)
        poller.start()
        try:
            self.prompt_loop()
        finally:
            self.stop.set()
            poller.join(timeout=5)

    def prompt_loop(self):
        while True:
            # Discard anything typed while we were polling; it was not aimed at
            # this prompt and would otherwise be sent to Jules verbatim.
            flush_input()
            label = self.prompt_label()
            set_prompt(label, True)
            try:
                raw = input(label)
            except (KeyboardInterrupt, EOFError):
                set_prompt("", False)
                say(COLOR_SYSTEM, "\nLeaving chat. The Jules session keeps running.")
                return
            finally:
                set_prompt("", False)

            text = clean_input(raw)
            if not text:
                self.sync()
                self.refresh_state()
                continue

            lowered = text.lower()

            if lowered in ("/exit", "/quit", "exit", "quit"):
                say(COLOR_SYSTEM, "Leaving chat. The Jules session keeps running.")
                return
            if lowered in ("/help", "/?"):
                self.show_help()
                continue
            if lowered == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
                continue
            if lowered in ("/verbose", "/v"):
                self.verbose = not self.verbose
                say(COLOR_SYSTEM, "Verbose {}.".format("on" if self.verbose else "off"))
                continue
            if lowered in ("/refresh", "/r"):
                if not self.sync():
                    say(COLOR_SYSTEM, "Nothing new.")
                self.refresh_state()
                continue
            if lowered in ("/wait", "/w"):
                self.wait_for_idle()
                continue
            if lowered in ("/state", "/status"):
                self.show_state()
                continue
            if lowered == "/plan":
                self.show_plan()
                continue
            if lowered == "/diff":
                self.show_diff()
                continue
            if lowered in ("/approve", "/a") or (
                self.state == "AWAITING_PLAN_APPROVAL"
                and lowered in ("y", "yes", "ok", "approve")
            ):
                self.approve()
                continue
            if lowered.startswith("/"):
                say(COLOR_SYSTEM, "Unknown command {}. Try /help.".format(text.split()[0]))
                continue

            try:
                self.client.send_message(self.session_name, text)
            except JulesError as exc:
                say(COLOR_ERROR, "Failed to send message: {}".format(exc))
                continue
            # No blocking wait: the poller streams the answer in above the
            # prompt, so the next message can be typed straight away.
            self.sent_at = time.time()
            say(COLOR_DIM, "  (sent)")


def print_sessions(sessions):
    if not sessions:
        say(COLOR_SYSTEM, "No sessions found.")
        return
    say(COLOR_SYSTEM, "Sessions:")
    for index, session in enumerate(sessions, start=1):
        name = session.get("name", session.get("id", "unknown"))
        title = (session.get("title") or session.get("prompt") or "No title").strip()
        title = title.splitlines()[0] if title else "No title"
        state = session.get("state", "UNKNOWN")
        if state == "AWAITING_PLAN_APPROVAL":
            color = COLOR_PLAN
        elif state in WAITING_STATES:
            color = COLOR_SYSTEM
        else:
            color = COLOR_DIM
        print("[{}] {}{:<24}{} {} - {}".format(
            index, color, state, COLOR_RESET, name, title[:60]
        ))


def resolve_session(selector, sessions):
    """Accept a 1-based list index, a bare session id, or a sessions/<id> name."""
    try:
        index = int(selector)
    except ValueError:
        return selector if selector.startswith("sessions/") else "sessions/" + selector
    if 1 <= index <= len(sessions):
        chosen = sessions[index - 1]
        return chosen.get("name") or "sessions/{}".format(chosen.get("id"))
    # A long number is a session id rather than an index into the list.
    if len(str(selector)) > 6:
        return "sessions/{}".format(selector)
    return None


def main():
    setup_console()
    parser = argparse.ArgumentParser(description="Jules CLI Chat")
    parser.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="LIST",
        help="Resume a session by list index or session id; omit the value to list sessions",
    )
    parser.add_argument("-l", "--list", action="store_true", help="List recent sessions and exit")
    args = parser.parse_args()

    if not args.resume and not args.list:
        parser.print_help()
        return

    client = JulesClient(get_headers(load_api_key()))
    try:
        sessions = client.list_sessions()
    except JulesError as exc:
        say(COLOR_ERROR, "Failed to fetch sessions: {}".format(exc))
        sys.exit(1)

    if args.list or args.resume == "LIST":
        print_sessions(sessions)
        return

    session_name = resolve_session(args.resume, sessions)
    if not session_name:
        say(COLOR_ERROR, "Invalid session index {}.".format(args.resume))
        print_sessions(sessions)
        sys.exit(1)

    ChatSession(client, session_name).run()


if __name__ == "__main__":
    main()
