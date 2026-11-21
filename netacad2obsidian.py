#!/usr/bin/env python3
"""
netacad2obsidian - read your own NetAcad course pages and turn them into Obsidian notes.

You log in manually in the browser window this opens (SSO / MFA work normally).
The script never sees or stores your password; the browser session is kept in ./profile
so you only have to log in once.

For personal study use only. Respect NetAcad's terms of service and don't redistribute
the course material.
"""
import argparse
import json
import queue
import subprocess
import threading
import hashlib
import os
import re
import sys
from datetime import date
from pathlib import Path

from markdownify import markdownify as to_md
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------------------
# Selectors you may need to tweak if NetAcad changes its layout
# --------------------------------------------------------------------------------------
NEXT_SELECTORS = [
    'button[aria-label*="next" i]',
    'a[aria-label*="next" i]',
    'button:has-text("Next")',
    'a:has-text("Next")',
    '[class*="next" i][role="button"]',
]

EXTRACT_JS = """
() => {
  const sels = ["main", "[role=main]", "article", "#content", ".content", "body"];
  let root = null;
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.innerText && el.innerText.trim().length > 150) { root = el; break; }
  }
  if (!root) return null;
  const c = root.cloneNode(true);
  c.querySelectorAll("script,style,nav,header,footer,button,noscript,svg,select,input")
   .forEach(e => e.remove());
  c.querySelectorAll("img").forEach(i => { if (i.src) i.setAttribute("src", i.src); });
  c.querySelectorAll("a").forEach(a => { if (a.href) a.setAttribute("href", a.href); });
  return { html: c.innerHTML, text: c.innerText };
}
"""


def slugify(text: str, maxlen: int = 80) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", " ", text)  # chars Obsidian dislikes in filenames
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maxlen].strip() or "Untitled"


NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s+(.*)$")

# Looks in the course sidebar/TOC for the lesson (e.g. "3.1 Physical Layer") that
# contains the page you're currently on.
LESSON_JS = """
() => {
  const cur = document.querySelector(
    'nav [aria-current], aside [aria-current], [role=tree] [aria-current],' +
    'nav .active, aside .active, nav .selected, aside .selected, [aria-selected=true]');
  if (!cur) return null;
  let el = cur;
  while (el && el !== document.body) {
    el = el.parentElement;
    if (!el) break;
    const head = el.querySelector(':scope > button, :scope > a, :scope > div > button, :scope > span, :scope > h3, :scope > h4');
    const t = head && head.innerText ? head.innerText.trim().split("\\n")[0] : "";
    if (/^\\d+\\.\\d+\\s+\\S/.test(t) && !/^\\d+\\.\\d+\\.\\d+/.test(t)) return t;
  }
  return null;
}
"""


def split_number(title: str):
    """'3.1.1 Connecting to the Network' -> ('3.1.1', 'Connecting to the Network')"""
    m = NUM_RE.match(title or "")
    return (m.group(1), m.group(2).strip()) if m else (None, (title or "").strip())


def lesson_key(num: str) -> str:
    """'3.1.4' -> '3.1' (the lesson); '3.1' -> '3.1'; '3' -> '3'."""
    return ".".join(num.split(".")[:2])


def find_lesson_name(page):
    for frame in page.frames:
        try:
            t = frame.evaluate(LESSON_JS)
        except Exception:
            continue
        if t:
            return split_number(t)[1]
    return None


def extract_page(page):
    """Collect content from the main page and every iframe (NetAcad nests content)."""
    chunks = []
    for frame in page.frames:
        try:
            result = frame.evaluate(EXTRACT_JS)
        except Exception:
            continue
        if result and result["text"].strip():
            chunks.append(result["html"])
    if not chunks:
        return "", ""
    html = "\n<hr>\n".join(chunks)
    markdown = to_md(html, heading_style="ATX", bullets="-", strip=["span", "div"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    m = re.search(r"^#{1,3}\s+(.+)$", markdown, re.M)
    title = m.group(1).strip() if m else ""
    if not title:
        try:
            title = page.title()
        except Exception:
            title = ""
    return title, markdown


def download_images(context, markdown: str, att_dir: Path) -> str:
    """Download images (using the logged-in session) and rewrite to Obsidian embeds."""
    def repl(match):
        alt, url = match.group(1), match.group(2).split(" ")[0]
        if not url.startswith("http"):
            return match.group(0)
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
            ext = ".png"
        name = hashlib.sha1(url.encode()).hexdigest()[:12] + ext
        target = att_dir / name
        if not target.exists():
            try:
                resp = context.request.get(url, timeout=15000)
                if not resp.ok:
                    return match.group(0)
                att_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(resp.body())
            except Exception:
                return match.group(0)
        return f"![[{name}]]"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, markdown)


def summarize(markdown: str) -> str:
    """Optional: ask Claude for takeaways + study questions."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    "Below are my study notes from a Cisco NetAcad page. Write two sections in "
                    "Markdown: '## Key takeaways' (3-6 bullets) and '## Study questions' "
                    "(3-5 questions, no answers). Output only those sections.\n\n" + markdown[:12000]
                ),
            }],
        )
        return "\n\n" + msg.content[0].text.strip() + "\n"
    except Exception as e:
        print(f"  (summary skipped: {e})")
        return ""


def click_next(page, custom_selector=None) -> bool:
    selectors = [custom_selector] if custom_selector else NEXT_SELECTORS
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue
    return False


def build_note(course, title, url, body, index, prev_t, next_t, summary):
    course_tag = re.sub(r"[^a-z0-9]+", "-", course.lower()).strip("-")
    fm = (
        "---\n"
        f'title: "{title.replace(chr(34), chr(39))}"\n'
        f'course: "{course}"\n'
        f"source: {url}\n"
        f"captured: {date.today().isoformat()}\n"
        f"order: {index}\n"
        f"tags: [netacad, {course_tag}]\n"
        "---\n\n"
    )
    nav = []
    if prev_t:
        nav.append(f"⬅️ [[{prev_t}]]")
    nav.append(f"[[{course} - Index|Index]]")
    if next_t:
        nav.append(f"[[{next_t}]] ➡️")
    return fm + body + summary + "\n\n---\n" + " | ".join(nav) + "\n"


def run(opts, log=print, wait_ready=None, stop_event=None):
    """Run a capture. Used by both the CLI and the GUI.

    opts: dict with vault, course, folder, start_url, max_pages, delay,
          next_selector, summarize, no_images
    wait_ready: callable that blocks until the user has logged in and opened the first
                page (GUI button / CLI Enter key).
    stop_event: optional threading.Event; when set, capture stops and what was
                collected so far is still written.
    Returns the output folder path.
    """
    out_dir = Path(opts["vault"]).expanduser() / opts.get("folder", "NetAcad") / slugify(opts["course"])
    out_dir.mkdir(parents=True, exist_ok=True)
    att_dir = out_dir / "attachments"
    course = opts["course"]

    notes = []  # (title, markdown_body, url, summary, group_folder)
    seen = set()
    groups = {}  # lesson key ("3.1") -> folder name ("02 - Physical Layer")
    current_key = None

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(__file__).parent / "profile"),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(opts.get("start_url") or "https://www.netacad.com")

            log("Log in to NetAcad in the browser window, then open the FIRST page you want to capture.")
            if wait_ready:
                wait_ready()

            for i in range(1, int(opts.get("max_pages", 200)) + 1):
                if stop_event and stop_event.is_set():
                    log("Stopped by you - saving what was captured so far.")
                    break
                page.wait_for_timeout(int(float(opts.get("delay", 2.5)) * 1000))
                title, body = extract_page(page)
                digest = hashlib.sha1(body.encode()).hexdigest()

                if not body:
                    log(f"[{i}] No content found - stopping (try a custom Next selector or check the page).")
                    break
                if digest in seen:
                    log(f"[{i}] Page didn't change - reached the end (or Next failed).")
                    break
                seen.add(digest)

                num, bare = split_number(title)
                key = lesson_key(num) if num else (current_key or "intro")
                if key != current_key:  # first page of a new lesson -> new group
                    current_key = key
                    name = find_lesson_name(page) or bare or f"Lesson {key}"
                    groups[key] = f"{len(groups) + 1:02d} - {slugify(name)}"
                    log(f"== New lesson: {groups[key]}")

                title = f"{i:03d} - {slugify(bare or title or f'Page {i}')}"
                log(f"[{i}] {title}")

                if not opts.get("no_images"):
                    body = download_images(context, body, att_dir)
                summary = summarize(body) if opts.get("summarize") else ""
                notes.append((title, body, page.url, summary, groups[key]))

                if not click_next(page, opts.get("next_selector") or None):
                    log("No Next button found - done.")
                    break
        finally:
            context.close()

    # Write notes with prev/next navigation
    for idx, (title, body, url, summary, group) in enumerate(notes):
        prev_t = notes[idx - 1][0] if idx > 0 else None
        next_t = notes[idx + 1][0] if idx < len(notes) - 1 else None
        text = build_note(course, title, url, body, idx + 1, prev_t, next_t, summary)
        group_dir = out_dir / group  # one folder per lesson, named after the lesson topic
        group_dir.mkdir(parents=True, exist_ok=True)
        (group_dir / f"{title}.md").write_text(text, encoding="utf-8")

    # Index / map-of-content note, grouped by lesson
    index_lines = ["---", "tags: [netacad, moc]", "---", "", f"# {course}", ""]
    last_group = None
    for title, _b, _u, _s, group in notes:
        if group != last_group:
            index_lines += ["", f"## {group.split(' - ', 1)[-1]}"]
            last_group = group
        index_lines.append(f"- [[{title}]]")
    (out_dir / f"{course} - Index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    log(f"Done. {len(notes)} notes written to {out_dir}")
    return out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert NetAcad course pages into Obsidian notes.")
    ap.add_argument("--vault", required=True, help="Path to your Obsidian vault")
    ap.add_argument("--course", required=True, help='Course name, e.g. "CCNA 1 - Module 3"')
    ap.add_argument("--folder", default="NetAcad", help="Subfolder inside the vault")
    ap.add_argument("--start-url", default="https://www.netacad.com", help="URL to open first")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--delay", type=float, default=2.5, help="Seconds to wait after each click")
    ap.add_argument("--next-selector", help="Custom CSS/Playwright selector for the Next button")
    ap.add_argument("--summarize", action="store_true",
                    help="Add takeaways + study questions via Claude (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args(argv)

    if args.summarize and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--summarize needs the ANTHROPIC_API_KEY environment variable.")

    run(vars(args), wait_ready=lambda: input("Press Enter here when you're ready... "))


# ======================================================================================
# GUI (needs tkinter - bundled with Python on Windows/macOS; Linux: sudo apt install python3-tk)
# ======================================================================================
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # CLI still works without tkinter
    tk = None

SETTINGS_FILE = Path(__file__).parent / "gui_settings.json"  # API key is never saved


class QueueWriter:
    """Redirects print() output from the worker thread into the GUI log."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s and s.strip():
            self.q.put(s.rstrip())

    def flush(self):
        pass


class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title("NetAcad → Obsidian")
        self.geometry("760x720")
        self.minsize(680, 620)

        self.q = queue.Queue()
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()
        self.worker = None
        self.out_dir = None
        self.page_count = 0

        self._build()
        self._load_settings()
        self._set_state("idle")
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", padding=(14, 8), font=("TkDefaultFont", 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666")

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        # --- Settings
        box = ttk.LabelFrame(root, text="Settings", padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        self.vault = tk.StringVar()
        self.course = tk.StringVar()
        self.folder = tk.StringVar(value="NetAcad")
        self.url = tk.StringVar(value="https://www.netacad.com")
        self.max_pages = tk.StringVar(value="200")
        self.delay = tk.StringVar(value="2.5")
        self.next_sel = tk.StringVar()
        self.api_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.summarize = tk.BooleanVar(value=False)
        self.no_images = tk.BooleanVar(value=False)

        r = 0
        ttk.Label(box, text="Obsidian vault").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.vault).grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(box, text="Browse…", command=self._browse).grid(row=r, column=2)

        r += 1
        ttk.Label(box, text="Course / module name").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.course).grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)

        r += 1
        ttk.Label(box, text="Vault subfolder").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.folder).grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)

        # --- Options
        opt = ttk.LabelFrame(root, text="Options", padding=10)
        opt.pack(fill="x", pady=(10, 0))
        opt.columnconfigure(1, weight=1)

        ttk.Label(opt, text="Start URL").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(opt, textvariable=self.url).grid(row=0, column=1, columnspan=3, sticky="ew", padx=6)

        ttk.Label(opt, text="Max pages").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(opt, from_=1, to=2000, textvariable=self.max_pages, width=8).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="Delay per page (s)").grid(row=1, column=2, sticky="e")
        ttk.Spinbox(opt, from_=0.5, to=30, increment=0.5, textvariable=self.delay, width=8).grid(row=1, column=3, sticky="w", padx=6)

        ttk.Label(opt, text="Next-button selector").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(opt, textvariable=self.next_sel).grid(row=2, column=1, columnspan=3, sticky="ew", padx=6)
        ttk.Label(opt, text="Leave blank to auto-detect. Only needed if capture stops after page 1.",
                  style="Hint.TLabel").grid(row=3, column=1, columnspan=3, sticky="w", padx=6)

        ttk.Checkbutton(opt, text="Don't download images", variable=self.no_images).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opt, text="Add takeaways + study questions with Claude", variable=self.summarize,
                        command=self._toggle_key).grid(row=5, column=0, columnspan=3, sticky="w")

        ttk.Label(opt, text="Anthropic API key").grid(row=6, column=0, sticky="w", pady=3)
        self.key_entry = ttk.Entry(opt, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=6, column=1, columnspan=3, sticky="ew", padx=6)
        ttk.Label(opt, text="Used only for this run; never saved to disk.", style="Hint.TLabel").grid(
            row=7, column=1, columnspan=3, sticky="w", padx=6)

        # --- Controls
        ctl = ttk.Frame(root)
        ctl.pack(fill="x", pady=12)
        self.btn_start = ttk.Button(ctl, text="1. Open NetAcad", style="Big.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_ready = ttk.Button(ctl, text="2. I'm on the first page — Begin", style="Big.TButton", command=self._ready)
        self.btn_ready.pack(side="left", padx=8)
        self.btn_stop = ttk.Button(ctl, text="Stop & save", command=self._stop)
        self.btn_stop.pack(side="left")
        self.btn_open = ttk.Button(ctl, text="Open output folder", command=self._open_out)
        self.btn_open.pack(side="right")

        self.status = tk.StringVar()
        ttk.Label(root, textvariable=self.status, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.bar = ttk.Progressbar(root, mode="indeterminate")
        self.bar.pack(fill="x", pady=(4, 8))

        # --- Log
        logf = ttk.LabelFrame(root, text="Log", padding=4)
        logf.pack(fill="both", expand=True)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled", font=("TkFixedFont", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._toggle_key()

    def _toggle_key(self):
        self.key_entry.configure(state="normal" if self.summarize.get() else "disabled")

    def _browse(self):
        d = filedialog.askdirectory(title="Choose your Obsidian vault folder")
        if d:
            self.vault.set(d)

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return
        self.vault.set(data.get("vault", ""))
        self.course.set(data.get("course", ""))
        self.folder.set(data.get("folder", "NetAcad"))
        self.url.set(data.get("url", self.url.get()))
        self.max_pages.set(data.get("max_pages", "200"))
        self.delay.set(data.get("delay", "2.5"))
        self.next_sel.set(data.get("next_sel", ""))
        self.no_images.set(data.get("no_images", False))
        self.summarize.set(data.get("summarize", False))
        self._toggle_key()

    def _save_settings(self):
        data = {
            "vault": self.vault.get(), "course": self.course.get(), "folder": self.folder.get(),
            "url": self.url.get(), "max_pages": self.max_pages.get(), "delay": self.delay.get(),
            "next_sel": self.next_sel.get(), "no_images": self.no_images.get(),
            "summarize": self.summarize.get(),
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    # --------------------------------------------------------------- state
    def _set_state(self, state):
        """idle -> browser (waiting for user) -> running -> idle"""
        self.state = state
        en = lambda b, on: b.configure(state="normal" if on else "disabled")
        en(self.btn_start, state == "idle")
        en(self.btn_ready, state == "browser")
        en(self.btn_stop, state in ("browser", "running"))
        en(self.btn_open, self.out_dir is not None and state == "idle")
        if state == "idle":
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)
        else:
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
        self.status.set({
            "idle": "Ready.",
            "browser": "Browser is open. Log in, go to the first page of the module, then click Begin.",
            "running": "Capturing…",
        }[state])

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------- actions
    def _validate(self):
        vault = Path(self.vault.get()).expanduser()
        if not vault.is_dir():
            messagebox.showerror("Vault", "Choose your Obsidian vault folder first.")
            return None
        if not self.course.get().strip():
            messagebox.showerror("Course name", "Enter a course or module name, e.g. 'CCNA 1 - Module 3'.")
            return None
        try:
            max_pages = int(self.max_pages.get())
            delay = float(self.delay.get())
        except ValueError:
            messagebox.showerror("Options", "Max pages must be a whole number and delay a number.")
            return None
        if self.summarize.get() and not self.api_key.get().strip():
            messagebox.showerror("API key", "Enter your Anthropic API key, or untick the Claude option.")
            return None
        return {
            "vault": str(vault), "course": self.course.get().strip(),
            "folder": self.folder.get().strip() or "NetAcad",
            "start_url": self.url.get().strip(), "max_pages": max_pages, "delay": delay,
            "next_selector": self.next_sel.get().strip() or None,
            "summarize": self.summarize.get(), "no_images": self.no_images.get(),
        }

    def _start(self):
        opts = self._validate()
        if not opts:
            return
        self._save_settings()
        if opts["summarize"]:
            os.environ["ANTHROPIC_API_KEY"] = self.api_key.get().strip()
        self.ready_event.clear()
        self.stop_event.clear()
        self.page_count = 0
        self.out_dir = None
        self._append("— Starting —")
        self._set_state("browser")
        self.worker = threading.Thread(target=self._work, args=(opts,), daemon=True)
        self.worker.start()

    def _ready(self):
        self._set_state("running")
        self.ready_event.set()

    def _stop(self):
        self.stop_event.set()
        self.ready_event.set()  # unblock if still waiting for the user
        self._append("Stopping…")

    def _open_out(self):
        if not self.out_dir:
            return
        p = str(self.out_dir)
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            messagebox.showerror("Open folder", str(e))

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A capture is running. Quit anyway? Unsaved pages will be lost."):
                return
        self.destroy()

    # -------------------------------------------------------------- worker
    def _work(self, opts):
        old_out = sys.stdout
        sys.stdout = QueueWriter(self.q)
        try:
            out = run(opts, log=lambda m: self.q.put(m),
                           wait_ready=self.ready_event.wait, stop_event=self.stop_event)
            self.q.put(("__done__", out))
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                msg += "\n\nRun this once in a terminal:  playwright install chromium"
            self.q.put(("__error__", msg))
        finally:
            sys.stdout = old_out

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    kind, payload = item
                    if kind == "__done__":
                        self.out_dir = payload
                        self._set_state("idle")
                        self.status.set(f"Finished — {self.page_count} pages saved.")
                    elif kind == "__error__":
                        self._append("ERROR: " + payload)
                        self._set_state("idle")
                        messagebox.showerror("Something went wrong", payload)
                else:
                    if re.match(r"^\[\d+\] \d{3} - ", item):
                        self.page_count += 1
                        self.status.set(f"Capturing… {item}")
                    self._append(item)
        except queue.Empty:
            pass
        self.after(100, self._poll)


def launch_gui():
    if tk is None:
        sys.exit("tkinter isn't installed. On Linux run: sudo apt install python3-tk  "
                 "(or use the command-line options, see --help).")
    App().mainloop()


if __name__ == "__main__":
    # No arguments -> open the window. Any arguments -> command-line mode.
    if len(sys.argv) == 1:
        launch_gui()
    else:
        main()
