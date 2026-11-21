# netacad2obsidian

Walks through your NetAcad course pages in a browser you control and writes Obsidian notes.

## Setup (once)

```bash
pip install playwright markdownify anthropic   # anthropic only needed for --summarize
playwright install chromium
```

## Run with the GUI (easiest)

```bash
python netacad2obsidian.py
```

Pick your vault, type a course name, click **1. Open NetAcad**, log in and open the first
page of the module in the browser that appears, then click **2. Begin**. Watch progress in
the log, use **Stop & save** to end early, and **Open output folder** when finished.
Your settings are remembered (the API key is not). Tkinter ships with Python on Windows
and macOS; on Linux you may need `sudo apt install python3-tk`.

## Run from the command line (add any option to skip the window)

```bash
python netacad2obsidian.py --vault ~/Documents/MyVault --course "CCNA 1 - Module 3"
```

1. A browser opens. Log in to NetAcad (your session is saved in `./profile`).
2. Open the first page of the module you want.
3. Press Enter in the terminal. It reads, clicks Next, and repeats until the end.

Notes land in `<vault>/NetAcad/<course>/`, grouped into one folder per lesson, named
after the lesson topic (e.g. `02 - Physical Layer/`), not its number (`3.1`). Pages are
grouped by their numbering (3.1.1, 3.1.2, ... all belong to lesson 3.1). The lesson name
is read from NetAcad's sidebar; if that can't be found, the first page's title is used.
The course root also has an `Index` note (grouped by lesson) and images in `attachments/`.

## Options

| Flag | Purpose |
|---|---|
| `--summarize` | Add Key takeaways + Study questions with Claude (set `ANTHROPIC_API_KEY`) |
| `--next-selector "button.my-next"` | Override Next-button detection |
| `--max-pages 50` | Limit pages per run |
| `--delay 4` | Wait longer after each click on slow connections |
| `--no-images` | Skip image downloads |
| `--folder Networking` | Change the vault subfolder |

## Troubleshooting

- **"No content found"**: the content may be in an unusual container. Edit `EXTRACT_JS` selectors at the top of the script.
- **Never clicks Next / stops after page 1**: right-click the Next button, Inspect, and pass its selector with `--next-selector`.
- **Quizzes/interactive labs** (Packet Tracer, drag-and-drop) aren't captured; only readable text and images are.
- Capture one module per run for tidier notes (use `--course "CCNA 1 - Module 4"` etc.).

For personal study only. Don't redistribute the course content.
