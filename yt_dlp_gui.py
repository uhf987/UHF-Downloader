import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import shlex
import shutil
import re
import sys
import queue
import json
import os
import time
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Sistem tepsisi desteği (pip install pystray pillow)
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

# exe (--noconsole) olarak çalışırken yt-dlp için konsol penceresi açılmasın
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Bu programın başlattığı alt süreçlere işaret koyulur. Yanlışlıkla program
# kendi kendini başlatırsa (yt-dlp yerine) hemen kapanır, pencere açmaz.
GUARD_VAR = "YTDLP_GUI_CHILD"
CHILD_ENV = dict(os.environ, **{GUARD_VAR: "1"})


def find_ytdlp():
    """
    PATH içinde yt-dlp'yi bulur ve TAM YOLUNU döndürür.
    Uygulamanın kendi exe'sini (adı yt-dlp.exe olsa bile) asla seçmez.
    Windows'un 'uygulama klasörü / çalışma klasörü önce aranır' davranışına
    güvenmemek için komut adı yerine tam yol kullanılır.
    """
    me = os.path.abspath(sys.executable)
    if os.name == "nt":
        names = ["yt-dlp.exe", "yt-dlp.cmd", "yt-dlp.bat"]
    else:
        names = ["yt-dlp"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            try:
                if os.path.samefile(p, me):
                    continue
            except OSError:
                pass
            return p
    return None


# ── Kuyruk durumları ───────────────────────────────────────────
ST_WAIT = "waiting"
ST_RUN = "downloading"
ST_MERGE = "merging"
ST_DONE = "completed"
ST_ERR = "error"
ST_CANCEL = "cancelled"
ACTIVE = (ST_WAIT, ST_RUN, ST_MERGE)
STATUS_TAG = {ST_WAIT: "wait", ST_RUN: "run", ST_MERGE: "merge",
              ST_DONE: "done", ST_ERR: "err", ST_CANCEL: "cancel"}

# yt-dlp ilerleme satırları:  [download]  42.3% of 100.00MiB at 5.00MiB/s ETA 00:12
PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
SPEED_RE = re.compile(r"\bat\s+(\S+/s)")
ETA_RE = re.compile(r"ETA\s+(\S+)")
# aria2c ilerleme satırları:  [#a1b2c3 3.2MiB/10MiB(32%) CN:16 DL:2.1MiB ETA:3s]
ARIA_RE = re.compile(r"\[#\w+\s+[\d.]+\w*/[\d.]+\w*\((\d+)%\)")
ARIA_DL_RE = re.compile(r"DL:([^\s\]]+)")
ARIA_ETA_RE = re.compile(r"ETA:([^\s\]]+)")
MERGE_PREFIXES = ("[Merger]", "[ExtractAudio]", "[VideoRemuxer]", "[FixupM3u8]",
                  "[FixupM4a]", "[FixupStretched]", "[FixupTimestamp]")


def kill_tree(proc):
    """İndirmeyi ve onun başlattığı ffmpeg/aria2c süreçlerini sonlandırır."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        else:
            proc.terminate()
    except Exception:
        pass


def short(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def nice_title(url):
    """Dosya adı henüz bilinmiyorsa listede görünecek kısa ad: alanadı/son-parça."""
    try:
        u = urlparse(url)
        tail = u.path.rstrip("/").split("/")[-1]
        return (u.netloc + "/" + tail) if tail else (u.netloc or url)
    except Exception:
        return url


def bar_text(pct, width=8):
    """Listede görünen metin tabanlı ilerleme çubuğu."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled) + f" {pct:3.0f}%"


def make_icon_image(size=64):
    """Tepsi simgesi: kırmızı yuvarlak kare içinde beyaz oynat üçgeni."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(255, 68, 68, 255))
    d.polygon([(24, 18), (24, 46), (48, 32)], fill=(255, 255, 255, 255))
    return img.resize((size, size)) if size != 64 else img

class Tip:
    """Düğmelerin üzerine gelince çıkan küçük ipucu balonu."""

    def __init__(self, widget, text):
        self.w, self.text, self.tw = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tw:
            return
        x = self.w.winfo_rootx() + 4
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tw, text=self.text, font=("Consolas", 8), bg="#333333",
                 fg="#f0f0f0", padx=6, pady=2, bd=1, relief="solid").pack()

    def _hide(self, _e=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


# ── Renkler & Font ──────────────────────────────────────────────
BG = "#0f0f0f"
CARD = "#1a1a1a"
ACCENT = "#ff4444"
ACCENT2 = "#ff6b35"
TEXT = "#f0f0f0"
MUTED = "#666666"
SUCCESS = "#44ff88"
BORDER = "#2a2a2a"
PIN_ON = "#ffcc00"
QUEUE_BG = "#111111"
FIELD = "#252525"
PAD = 10

FONT_TITLE = ("Consolas", 14, "bold")
FONT_LABEL = ("Consolas", 9)
FONT_BTN = ("Consolas", 10, "bold")
FONT_LOG = ("Consolas", 9)
FONT_SMALL = ("Consolas", 8)

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")

# Eklentinin gönderebildiği başlıklar
KNOWN_HEADERS = ("referer", "origin", "user-agent", "cookie")

# Eklenti ile haberleşen yerel sunucu (sadece bu bilgisayardan erişilebilir)
SERVER_PORT = 8765

# İndirme hızı modları: (yt-dlp -N parça sayısı, aria2c kullanılsın mı)
#  -N  : HLS/DASH (m3u8/mpd) parçalarını aynı anda N bağlantıyla indirir
#  aria2c : düz dosyaları (mp4 vb.) çok bağlantıyla bölerek indirir, IDM'e en yakın yöntem
SPEED_MODES = {
    "Normal (1 connection)": (1, False),
    "Fast (8 parts)": (8, False),
    "Very fast (16 + aria2c)": (16, True),
}
SPEED_DEFAULT = "Very fast (16 + aria2c)"


def make_handler(app):
    """Eklentiden gelen istekleri alan HTTP işleyicisi."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # konsolu kirletme

        def _allowed(self):
            # DNS rebinding'e karşı Host kontrolü
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}"):
                return False
            # Web siteleri Origin başlığını taklit edemez: sadece eklenti ya da
            # tarayıcı dışı yerel istemciler (Origin yok) kabul edilir
            origin = self.headers.get("Origin", "")
            return origin == "" or origin.startswith("chrome-extension://")

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self._allowed():
                return self._send(403, {"ok": False, "error": "forbidden"})
            self._send(200, {"ok": True})

        def do_GET(self):
            if not self._allowed():
                return self._send(403, {"ok": False, "error": "forbidden"})
            if self.path == "/ping":
                try:
                    jobs = list(app._jobs_list)
                    running = sum(1 for j in jobs if j["status"] in (ST_RUN, ST_MERGE))
                    waiting = sum(1 for j in jobs if j["status"] == ST_WAIT)
                except Exception:
                    running = waiting = 0
                return self._send(200, {"ok": True, "busy": running > 0,
                                        "running": running, "waiting": waiting})
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._allowed():
                return self._send(403, {"ok": False, "error": "forbidden"})
            if self.path != "/add":
                return self._send(404, {"ok": False, "error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 64 * 1024:
                    return self._send(400, {"ok": False, "error": "bad size"})
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                url = str(data.get("url", "")).strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    return self._send(400, {"ok": False, "error": "bad url"})
                headers = {}
                for k, v in (data.get("headers") or {}).items():
                    if str(k).lower() in KNOWN_HEADERS and isinstance(v, str) and v:
                        headers[str(k)] = v
            except Exception:
                return self._send(400, {"ok": False, "error": "bad request"})

            # Meşgul olsa bile kabul edilir: iş kuyruğa eklenir
            app._jobs.put((url, headers))
            self._send(200, {"ok": True, "queued": True})

    return Handler


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UHF Downloader")
        self.geometry("600x540")
        self.resizable(True, True)
        self.minsize(540, 400)
        self.configure(bg=BG)

        self.download_dir = tk.StringVar(value=DEFAULT_DIR)
        self.quality = tk.StringVar(value="best")
        self.cookies = tk.StringVar(value="")
        self.speed = tk.StringVar(value=SPEED_DEFAULT)
        self._last_clipboard = ""
        self._auto_dl = tk.BooleanVar(value=False)      # otomatik indir (varsayılan KAPALI)
        self._always_top = tk.BooleanVar(value=False)   # her zaman üstte

        # İndirme kuyruğu
        self._jobs_list = []            # arayüzde görünen işler (sadece ana iş parçacığı değiştirir)
        self._pending = queue.Queue()   # indirme iş parçacığının sırası
        self._next_id = 0
        self._logged_ytdlp = None

        # Eklentiden gelen ekstra başlıklar (sadece o URL için geçerli)
        self._hdrs = {}
        self._hdrs_url = ""

        # Eklenti sunucusundan gelen işler
        self._jobs = queue.Queue()

        # Tepsi simgesi (başka iş parçacıklarından gelen arayüz komutları)
        self._tray = None
        self._ui_q = queue.Queue()
        self.protocol("WM_DELETE_WINDOW", self._quit)

        self._build()
        self._start_server()
        self._poll_clipboard()
        self._poll_jobs()
        self._poll_ui()
        self._refresh_tree()
        threading.Thread(target=self._worker_loop, daemon=True).start()

    # ── Arayüz ─────────────────────────────────────────────────
    def _btn(self, parent, text, cmd, bg=BORDER, fg=MUTED, abg=ACCENT,
             afg="white", padx=7, pady=2, font=FONT_SMALL):
        return tk.Button(parent, text=text, font=font, bg=bg, fg=fg,
                         activebackground=abg, activeforeground=afg, bd=0,
                         cursor="hand2", padx=padx, pady=pady, command=cmd)

    def _entry(self, parent, var, **kw):
        return tk.Entry(parent, textvariable=var, font=FONT_LABEL, bg=FIELD,
                        fg=TEXT, insertbackground=ACCENT, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT, **kw)

    def _combo(self, parent, var, values, width):
        return ttk.Combobox(parent, textvariable=var, values=values,
                            state="readonly", width=width, font=FONT_LABEL,
                            style="D.TCombobox")

    def _style_widgets(self):
        """Koyu tema: combobox, kaydırma çubuğu ve kuyruk listesi."""
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("D.TCombobox", fieldbackground=FIELD, background=BORDER,
                     foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                     lightcolor=FIELD, darkcolor=FIELD, selectbackground=FIELD,
                     selectforeground=TEXT, padding=2)
        st.map("D.TCombobox",
               fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", FIELD)],
               selectforeground=[("readonly", TEXT)],
               arrowcolor=[("active", ACCENT)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.option_add("*TCombobox*Listbox.font", FONT_LABEL)
        st.configure("Vertical.TScrollbar", background=BORDER, troughcolor=QUEUE_BG,
                     bordercolor=QUEUE_BG, arrowcolor=MUTED, lightcolor=BORDER,
                     darkcolor=BORDER, gripcount=0, arrowsize=11)
        st.map("Vertical.TScrollbar", background=[("active", MUTED)])
        st.configure("Q.Treeview", background=QUEUE_BG, fieldbackground=QUEUE_BG,
                     foreground=TEXT, rowheight=20, borderwidth=0, font=FONT_LOG)
        st.map("Q.Treeview", background=[("selected", "#333333")],
               foreground=[("selected", TEXT)])
        st.configure("Q.Treeview.Heading", background=BORDER, foreground=MUTED,
                     font=FONT_SMALL, relief="flat", borderwidth=0)
        st.map("Q.Treeview.Heading", background=[("active", BORDER)])

    def _build(self):
        self._style_widgets()
        self._log_open = False

        # ── Üst çubuk: başlık + kontrol düğmeleri (tek satır)
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=PAD, pady=(8, 6))
        tk.Label(top, text="▶ UHF", font=FONT_TITLE,
                 fg=ACCENT, bg=BG).pack(side="left")
        tk.Label(top, text=" Downloader", font=FONT_TITLE,
                 fg=TEXT, bg=BG).pack(side="left")

        self.btn_tray = self._btn(top, "🗕", self._to_tray, padx=8)
        self.btn_tray.pack(side="right")
        self.btn_pin = self._btn(top, "📌 TOP", self._toggle_pin, abg=PIN_ON, afg="black")
        self.btn_pin.pack(side="right", padx=(0, 4))
        # OTO varsayılan olarak kapalı başlar
        self.btn_auto = self._btn(top, "⚡ AUTO", self._toggle_auto, abg=ACCENT2)
        self.btn_auto.pack(side="right", padx=(0, 4))
        Tip(self.btn_tray, "Minimize to system tray")
        Tip(self.btn_pin, "Keep window always on top")
        Tip(self.btn_auto, "Auto-download links copied to clipboard")

        self._card_link()
        self._card_settings()

        # Sabit boyutlu alt kısım önce yerleşir; pencere küçülürse sıkışan
        # taraf kuyruk listesi olur.
        self._status_bar()
        self._card_log()
        self._card_queue()

    def _card_link(self):
        card = tk.Frame(self, bg=CARD, padx=8, pady=8)
        card.pack(fill="x", padx=PAD, pady=(0, 6))

        self.url_var = tk.StringVar()
        self.entry = self._entry(card, self.url_var)
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)

        self._btn(card, "PASTE", self._paste, padx=8, pady=5
                  ).pack(side="left", padx=(6, 0))

        self.btn_dl = tk.Button(
            card, text="⬇ DOWNLOAD", font=FONT_BTN, bg=ACCENT, fg="white",
            activebackground=ACCENT2, activeforeground="white", bd=0,
            cursor="hand2", padx=14, pady=3, command=self._start_download)
        self.btn_dl.pack(side="left", padx=(6, 0))
        Tip(self.entry, "Video link (filled in automatically from clipboard)")

    def _card_settings(self):
        card = tk.Frame(self, bg=CARD, padx=8, pady=6)
        card.pack(fill="x", padx=PAD, pady=(0, 6))

        # Satır 1: kayıt klasörü
        r1 = tk.Frame(card, bg=CARD)
        r1.pack(fill="x")
        tk.Label(r1, text="FOLDER", font=FONT_SMALL, fg=MUTED, bg=CARD,
                 width=6, anchor="w").pack(side="left")
        self._btn(r1, "BROWSE", self._choose_dir, padx=8
                  ).pack(side="right", padx=(6, 0))
        self._entry(r1, self.download_dir).pack(
            side="left", fill="x", expand=True, ipady=2)

        # Satır 2: kalite · hız · çerez
        r2 = tk.Frame(card, bg=CARD)
        r2.pack(fill="x", pady=(6, 0))
        tk.Label(r2, text="QUALITY", font=FONT_SMALL, fg=MUTED, bg=CARD,
                 width=6, anchor="w").pack(side="left")
        self._combo(r2, self.quality,
                    ["best", "1080p", "720p", "480p", "audio only (mp3)"], 12
                    ).pack(side="left")
        tk.Label(r2, text="SPEED", font=FONT_SMALL, fg=MUTED, bg=CARD
                 ).pack(side="left", padx=(10, 4))
        sp = self._combo(r2, self.speed, list(SPEED_MODES.keys()), 20)
        sp.pack(side="left")
        Tip(sp, "Segmented download (like IDM). Very fast uses aria2c if installed")

        tk.Label(r2, text="COOKIES", font=FONT_SMALL, fg=MUTED, bg=CARD
                 ).pack(side="left", padx=(10, 4))
        self._btn(r2, "BROWSE", self._choose_cookies, padx=8
                  ).pack(side="right", padx=(4, 0))
        ck = self._entry(r2, self.cookies, width=8)
        ck.pack(side="left", fill="x", expand=True, ipady=2)
        Tip(ck, "cookies.txt file. If empty, cookies are taken from Firefox")

    def _status_bar(self):
        """Altta tek satır: son çıktı satırı + çıktı kutusunu aç/kapat."""
        bar = tk.Frame(self, bg=BG)
        bar.pack(side="bottom", fill="x", padx=PAD, pady=(0, 6))
        self.btn_log = self._btn(bar, "▸ LOG", self._toggle_log, padx=6, pady=1)
        self.btn_log.pack(side="right")
        self._btn(bar, "🗑", self._clear_log, padx=6, pady=1
                  ).pack(side="right", padx=(0, 4))
        self.lbl_status = tk.Label(bar, text="Ready", font=FONT_SMALL,
                                   fg=MUTED, bg=BG, anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)

    def _card_queue(self):
        card = tk.Frame(self, bg=CARD, padx=8, pady=6)
        card.pack(fill="both", expand=True, padx=PAD, pady=(0, 6))
        self.queue_card = card

        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="QUEUE", font=FONT_SMALL, fg=TEXT,
                 bg=CARD).pack(side="left")
        self.lbl_queue = tk.Label(head, text="Queue empty", font=FONT_SMALL,
                                  fg=MUTED, bg=CARD, anchor="w")
        self.lbl_queue.pack(side="left", padx=(8, 0))

        # Sağdan sola dizildiği için ters sırayla ekleniyor
        for text, tip, cmd in reversed([
            ("⏹", "Cancel", self._cancel_selected),
            ("↻", "Retry", self._retry_selected),
            ("✕", "Remove from list", self._remove_selected),
            ("🧹", "Clear finished", self._clear_finished),
            ("📂", "Open folder", self._open_selected_folder),
        ]):
            b = self._btn(head, text, cmd, padx=6, pady=1)
            b.pack(side="right", padx=(3, 0))
            Tip(b, tip)

        wrap = tk.Frame(card, bg=QUEUE_BG)
        wrap.pack(fill="both", expand=True, pady=(6, 0))

        cols = ("name", "status", "progress", "speed", "eta")
        heads = ("FILE", "STATUS", "PROGRESS", "SPEED", "ETA")
        widths = (140, 105, 105, 70, 50)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="extended", height=5,
                                 style="Q.Treeview")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, anchor="w")
            self.tree.column(c, width=w, minwidth=40, anchor="w",
                             stretch=(c == "name"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        for tag, color in (("wait", "#aaaaaa"), ("run", TEXT), ("merge", ACCENT2),
                           ("done", SUCCESS), ("err", ACCENT), ("cancel", MUTED)):
            self.tree.tag_configure(tag, foreground=color)

        # Sağ tık menüsü ve Delete tuşu
        self.qmenu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT,
                             activebackground=ACCENT, activeforeground="white", bd=0)
        self.qmenu.add_command(label="⏹ Cancel", command=self._cancel_selected)
        self.qmenu.add_command(label="↻ Retry", command=self._retry_selected)
        self.qmenu.add_command(label="✕ Remove from list", command=self._remove_selected)
        self.qmenu.add_separator()
        self.qmenu.add_command(label="📂 Open folder", command=self._open_selected_folder)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())

    def _card_log(self):
        """Çıktı kutusu varsayılan olarak gizli; '▸ LOG' ile açılır."""
        card = tk.Frame(self, bg=CARD, padx=6, pady=6)
        self.log_card = card          # pack edilmez, _toggle_log gösterir
        sb = ttk.Scrollbar(card, orient="vertical")
        self.log = tk.Text(
            card, font=FONT_LOG, bg=QUEUE_BG, fg="#aaaaaa", relief="flat",
            bd=0, state="disabled", height=6, wrap="word",
            yscrollcommand=sb.set)
        sb.config(command=self.log.yview)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_config("ok", foreground=SUCCESS)
        self.log.tag_config("err", foreground=ACCENT)
        self.log.tag_config("inf", foreground="#aaaaff")

    def _toggle_log(self):
        if self._log_open:
            self.log_card.pack_forget()
            self.btn_log.config(text="▸ LOG")
        else:
            self.log_card.pack(side="bottom", fill="x", padx=PAD, pady=(0, 4),
                               before=self.queue_card)
            self.log.see("end")
            self.btn_log.config(text="▾ LOG")
        self._log_open = not self._log_open

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.lbl_status.config(text="Ready", fg=MUTED)

    # ── Toggle'lar ─────────────────────────────────────────────
    def _toggle_auto(self):
        self._auto_dl.set(not self._auto_dl.get())
        if self._auto_dl.get():
            self.btn_auto.config(bg=ACCENT, fg="white", text="⚡ AUTO")
        else:
            self.btn_auto.config(bg=BORDER, fg=MUTED, text="⚡ AUTO")

    def _toggle_pin(self):
        self._always_top.set(not self._always_top.get())
        if self._always_top.get():
            self.attributes("-topmost", True)
            self.btn_pin.config(bg=PIN_ON, fg="black", text="📌 TOP")
        else:
            self.attributes("-topmost", False)
            self.btn_pin.config(bg=BORDER, fg=MUTED, text="📌 TOP")

    # ── Sistem tepsisi ─────────────────────────────────────────
    def _to_tray(self):
        if not HAS_TRAY:
            messagebox.showinfo(
                "No tray support",
                "To minimize to the tray, install:\n\n"
                "pip install pystray pillow")
            return
        if self._tray is None:
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self._ui_q.put(("show", "")),
                                 default=True),
                pystray.MenuItem("Quit", lambda: self._ui_q.put(("quit", ""))),
            )
            self._tray = pystray.Icon(
                "uhf_downloader", make_icon_image(), "UHF Downloader", menu)
            self._tray.run_detached()
        self.withdraw()

    def _from_tray(self):
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.deiconify()
        self.lift()
        self.focus_force()

    def _notify(self, text):
        """Pencere tepsideyken küçük bir bildirim gösterir."""
        if self._tray is not None:
            try:
                self._tray.notify(text, "UHF Downloader")
            except Exception:
                pass

    def _quit(self):
        active = [j for j in self._jobs_list if j["status"] in ACTIVE]
        if active and not messagebox.askyesno(
                "Quit", f"{len(active)} download(s) running or waiting.\n"
                         "Quit anyway?"):
            return
        for j in self._jobs_list:
            if j.get("proc"):
                j["cancel"] = True
                kill_tree(j["proc"])
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.destroy()

    def _poll_ui(self):
        """Tepsi menüsünden ve indirme iş parçacığından gelen komutlar."""
        dirty = False
        try:
            while True:
                cmd, arg = self._ui_q.get_nowait()
                if cmd == "show":
                    self._from_tray()
                elif cmd == "quit":
                    self.after(0, self._quit)
                elif cmd == "notify":
                    self._notify(arg)
                elif cmd == "log":
                    self._write_log(*arg)
                elif cmd == "refresh":
                    dirty = True
                elif cmd == "done":
                    dirty = True
                    self._on_job_done(arg)
        except queue.Empty:
            pass
        if dirty:
            self._refresh_tree()
        self.after(200, self._poll_ui)

    # ── Eklenti sunucusu ───────────────────────────────────────
    def _start_server(self):
        try:
            self._server = ThreadingHTTPServer(
                ("127.0.0.1", SERVER_PORT), make_handler(self))
            threading.Thread(target=self._server.serve_forever,
                             daemon=True).start()
            self._write_log(
                f"🌐 Extension link ready (127.0.0.1:{SERVER_PORT})\n", "inf")
        except OSError:
            self._server = None
            self._write_log(
                f"⚠ Port {SERVER_PORT} is in use, sending from the extension will not work. "
                "Another copy of the app may be open.\n", "err")

    def _poll_jobs(self):
        """Eklentiden 'Araca gönder' ile gelen işleri kuyruğa ekle."""
        try:
            while True:
                url, hdrs = self._jobs.get_nowait()
                self._enqueue(url, hdrs)
        except queue.Empty:
            pass
        self.after(300, self._poll_jobs)

    # ── Pano / başlık ayrıştırma ───────────────────────────────
    def _parse_clip(self, text):
        """
        Pano metnini (url, başlıklar) olarak ayırır.
        Düz link  -> (link, {})            (eski davranışla aynı)
        Eklenti   -> ilk satır link, sonraki satırlar 'Referer: ...' gibi.
        'yt-dlp ...' komutu da çözülür.
        http ile başlamıyorsa (None, {}) döner.
        """
        text = text.strip()

        if text.startswith("yt-dlp "):
            return self._parse_command(text)

        if not (text.startswith("http://") or text.startswith("https://")):
            return None, {}

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) == 1:
            return lines[0], {}

        headers = {}
        for l in lines[1:]:
            name, sep, val = l.partition(":")
            if sep and name.strip().lower() in KNOWN_HEADERS:
                headers[name.strip()] = val.strip()
        return lines[0], headers

    def _parse_command(self, text):
        """'yt-dlp --referer "..." ... "URL"' metninden (url, başlıklar) çıkarır."""
        try:
            toks = shlex.split(text)
        except ValueError:
            return None, {}

        url = None
        headers = {}
        i = 1
        while i < len(toks):
            t = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if t == "--referer":
                headers["Referer"] = nxt
                i += 2
            elif t == "--user-agent":
                headers["User-Agent"] = nxt
                i += 2
            elif t == "--add-header":
                name, sep, val = nxt.partition(":")
                if sep and name.strip().lower() in KNOWN_HEADERS:
                    headers[name.strip()] = val.strip()
                i += 2
            elif t.startswith(("http://", "https://")):
                url = t
                i += 1
            else:
                i += 1
        if not url:
            return None, {}
        return url, headers

    def _set_url(self, url, headers):
        self.url_var.set(url)
        self._hdrs = headers
        self._hdrs_url = url if headers else ""

    def _header_args(self, headers):
        """Bir işin başlıklarını yt-dlp argümanlarına çevirir (düz linklerde boş)."""
        args = []
        for name, val in (headers or {}).items():
            n = name.lower()
            if n == "referer":
                args += ["--referer", val]
            elif n == "user-agent":
                args += ["--user-agent", val]
            else:
                args += ["--add-header", f"{name}: {val}"]
        return args

    # ── Yardımcılar ────────────────────────────────────────────
    def _paste(self):
        try:
            url, hdrs = self._parse_clip(self.clipboard_get())
            if url:
                self._set_url(url, hdrs)
            else:
                self._set_url(self.clipboard_get(), {})
        except Exception:
            pass

    def _poll_clipboard(self):
        """Her 500ms'de panoyu kontrol et; yeni link varsa yapıştır ve gerekirse kuyruğa ekle."""
        try:
            raw = self.clipboard_get()
            text = raw.strip()
            if text != self._last_clipboard:
                self._last_clipboard = text
                url, hdrs = self._parse_clip(text)
                if url:
                    self._set_url(url, hdrs)
                    if self._auto_dl.get():
                        self._start_download()   # kuyruğa ekler (aynı link iki kez eklenmez)
        except Exception:
            pass
        self.after(500, self._poll_clipboard)

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.download_dir.get())
        if d:
            self.download_dir.set(d)

    def _choose_cookies(self):
        f = filedialog.askopenfilename(
            filetypes=[("Text/cookies", "*.txt"), ("All files", "*.*")]
        )
        if f:
            self.cookies.set(f)

    def _write_log(self, text, tag=""):
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")
        # Çıktı kutusu kapalıyken de son satır alt çubukta görünsün
        last = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), "")
        if last and hasattr(self, "lbl_status"):
            color = {"ok": SUCCESS, "err": ACCENT, "inf": "#aaaaff"}.get(tag, MUTED)
            self.lbl_status.config(text=short(last, 90), fg=color)

    def _unique_path(self, path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 2
        while True:
            candidate = f"{base} ({i}){ext}"
            if not os.path.exists(candidate):
                return candidate
            i += 1

    def _cookie_args(self, ck):
        """
        Kullanıcı elle bir cookies.txt seçtiyse onu kullan.
        Seçmediyse, otomatik olarak Firefox tarayıcısının çerezlerini kullan.
        Firefox, Chrome/Edge'deki App-Bound Encryption korumasını
        kullanmadığı için yt-dlp çerezleri hiçbir eklentiye gerek
        kalmadan doğrudan okuyabilir.
        """
        if ck:
            return ["--cookies", ck]
        return ["--cookies-from-browser", "firefox"]

    # ── İndirme argümanları ────────────────────────────────────
    def _format_args(self, q):
        fmt = {
            "best": "bestvideo+bestaudio/best",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "audio only (mp3)": "bestaudio/best",
        }.get(q, "bestvideo+bestaudio/best")
        if q == "audio only (mp3)":
            return ["-x", "--audio-format", "mp3"]
        return ["-f", fmt]

    def _speed_args(self, mode):
        """
        Seçilen hız moduna göre yt-dlp argümanları ve log mesajı döndürür.
        -N: HLS/DASH parçalarını paralel indirir (m3u8/mpd için asıl hızlandırma).
        aria2c: PATH'te varsa düz dosyaları çok bağlantıyla indirir; m3u8/mpd
        için yt-dlp'nin kendi indiricisi kalır (daha güvenilir).
        """
        n, want_aria = SPEED_MODES.get(mode, SPEED_MODES[SPEED_DEFAULT])
        if n <= 1:
            return [], "Speed: normal (single connection)"
        args = ["-N", str(n)]
        msg = f"Speed: {n} parts at once"
        if want_aria:
            aria = shutil.which("aria2c")
            if aria:
                args += ["--downloader", "aria2c",
                         "--downloader", "dash,m3u8:native",
                         "--downloader-args", "aria2c:-x16 -s16 -k1M"]
                msg += " + aria2c (for plain files)"
            else:
                msg += " (aria2c not found, segmented download only)"
        return args, msg

    # ── Kuyruk ─────────────────────────────────────────────────
    def _start_download(self):
        """İNDİR düğmesi / OTO: kutudaki bağlantıyı kuyruğa ekler."""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a link.")
            return
        hdrs = self._hdrs if url == self._hdrs_url else {}
        self._enqueue(url, hdrs)

    def _enqueue(self, url, headers=None):
        """
        Yeni bir iş oluşturur ve kuyruğa ekler. İşin ayarları (kalite, hız,
        klasör, cookies, başlıklar) eklendiği andaki değerlerle saklanır;
        sonradan değiştirmek kuyruktaki işleri etkilemez.
        """
        url = url.strip()
        headers = dict(headers or {})
        for j in self._jobs_list:
            if j["url"] == url and j["status"] in ACTIVE:
                self._write_log("↪ This link is already in the queue, skipped.\n", "inf")
                return None

        self._next_id += 1
        job = {
            "id": self._next_id, "url": url, "headers": headers,
            "quality": self.quality.get(), "cookies": self.cookies.get().strip(),
            "speed": self.speed.get(), "outdir": self.download_dir.get(),
            "status": ST_WAIT, "percent": 0.0, "speed_txt": "", "eta": "",
            "title": nice_title(url), "msg": "", "note": "", "stage": 0,
            "proc": None, "cancel": False, "out_path": "",
        }
        self._jobs_list.append(job)
        self._pending.put(job)
        extra = f" (headers: {', '.join(headers)})" if headers else ""
        self._write_log(f"➕ [#{job['id']}] Added to queue: {short(url, 80)}{extra}\n", "inf")
        self._refresh_tree()
        return job

    def _worker_loop(self):
        """Kuyruktaki işleri sırayla indirir (aynı anda tek indirme)."""
        while True:
            job = self._pending.get()
            if job["status"] != ST_WAIT:
                continue   # iptal edilmiş, kaldırılmış ya da zaten çalışmış
            try:
                self._run_job(job)
            except Exception as e:   # iş parçacığı asla ölmesin
                job["status"] = ST_ERR
                job["msg"] = str(e)
                self._ui_q.put(("done", job))

    def _get_filename(self, ytdlp, job, hdr_args):
        out = os.path.join(job["outdir"], "%(title)s.%(ext)s")
        cmd = [ytdlp, "--ignore-config", "--print", "filename",
               "--simulate", "-o", out]
        cmd += self._format_args(job["quality"])
        cmd += self._cookie_args(job["cookies"])
        cmd += hdr_args
        cmd.append(job["url"])
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                env=CHILD_ENV, text=True, encoding="utf-8", errors="replace"
            )
            job["proc"] = proc          # bu aşamada da iptal edilebilsin
            stdout, _ = proc.communicate()
            job["proc"] = None
            for l in reversed(stdout.strip().splitlines()):
                l = l.strip()
                if l and not l.startswith("["):
                    return l
        except Exception:
            job["proc"] = None
        return None

    def _parse_progress(self, job, line):
        """yt-dlp / aria2c ilerleme satırını işe yazar. Satır ilerleme satırıysa True döner."""
        m = PROGRESS_RE.search(line)
        if m:
            job["percent"] = float(m.group(1))
            s = SPEED_RE.search(line)
            e = ETA_RE.search(line)
            job["speed_txt"] = s.group(1) if s else ""
            job["eta"] = e.group(1) if e else ""
            if job["status"] == ST_MERGE:
                job["status"] = ST_RUN
            return True
        m = ARIA_RE.search(line)
        if m:
            job["percent"] = float(m.group(1))
            s = ARIA_DL_RE.search(line)
            e = ARIA_ETA_RE.search(line)
            job["speed_txt"] = (s.group(1) + "/s") if s else ""
            job["eta"] = e.group(1) if e else ""
            return True
        return False

    def _run_job(self, job):
        jid = job["id"]

        def log(text, tag=""):
            self._ui_q.put(("log", (f"[#{jid}] {text}", tag)))

        def touch():
            self._ui_q.put(("refresh", None))

        status = ST_ERR
        try:
            job["status"] = ST_RUN
            job["note"] = "preparing"
            touch()

            ytdlp = find_ytdlp()
            if not ytdlp:
                job["msg"] = "yt-dlp not found (yt-dlp.exe must be in PATH)"
                log("❌ " + job["msg"] + "\n", "err")
                return
            if ytdlp != self._logged_ytdlp:
                self._logged_ytdlp = ytdlp
                log(f"🔧 yt-dlp: {ytdlp}\n", "inf")

            hdr_args = self._header_args(job["headers"])
            if hdr_args:
                log("🔑 Using headers from the extension: "
                    + ", ".join(job["headers"].keys()) + "\n", "inf")
            speed_args, speed_msg = self._speed_args(job["speed"])
            log(f"⚡ {speed_msg}\n", "inf")

            predicted = self._get_filename(ytdlp, job, hdr_args)
            if job["cancel"]:
                status = ST_CANCEL
                return

            if predicted:
                out_path = self._unique_path(predicted)
                if out_path != predicted:
                    log(f"📋 File already exists → {os.path.basename(out_path)}\n", "inf")
                job["title"] = os.path.basename(out_path)
                job["out_path"] = out_path
                out_arg = out_path.replace("%", "%%")   # -o şablonunda '%' özel karakterdir
            else:
                out_arg = os.path.join(job["outdir"], "%(title)s.%(ext)s")

            job["note"] = ""
            touch()
            log("▶ Starting download...\n", "inf")

            cmd = [ytdlp, "--ignore-config", "--newline"] + self._format_args(job["quality"])
            cmd += ["-o", out_arg]
            cmd += self._cookie_args(job["cookies"])
            cmd += hdr_args
            cmd += speed_args
            cmd.append(job["url"])

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                env=CHILD_ENV,
                text=True, encoding="utf-8", errors="replace"
            )
            job["proc"] = proc
            if job["cancel"]:       # hazırlık sırasında iptal edildiyse
                kill_tree(proc)

            last = 0.0
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                if self._parse_progress(job, line):
                    now = time.time()
                    if now - last > 0.2:
                        last = now
                        touch()
                    continue            # ilerleme satırları çıktıya yazılmaz (liste gösteriyor)
                if line.startswith("[download] Destination"):
                    job["stage"] += 1   # görüntü ve ses ayrı indiriliyorsa 2. akış
                    job["percent"] = 0.0
                    job["status"] = ST_RUN
                    touch()
                elif line.startswith(MERGE_PREFIXES):
                    job["status"] = ST_MERGE
                    job["percent"] = 100.0
                    job["speed_txt"] = ""
                    job["eta"] = ""
                    touch()
                if "ERROR" in line:
                    job["msg"] = line.split("ERROR:", 1)[-1].strip()
                tag = "err" if "ERROR" in line else \
                    "ok" if ("[download]" in line or "Destination" in line) else ""
                log(line + "\n", tag)
            proc.wait()

            rc = proc.returncode
            if rc == 0:
                status = ST_DONE
                job["percent"] = 100.0
            elif job["cancel"]:
                status = ST_CANCEL
            else:
                status = ST_ERR
                if rc == 3:
                    job["msg"] = ("This program ran itself instead of yt-dlp. The exe must not be named "
                                  "'yt-dlp.exe'; also check the yt-dlp.exe in PATH.")
                elif not job["msg"]:
                    job["msg"] = f"yt-dlp error code {rc}"
        except FileNotFoundError:
            job["msg"] = "yt-dlp not found! Is it added to PATH?"
        except Exception as e:
            job["msg"] = str(e)
        finally:
            job["proc"] = None
            job["note"] = ""
            job["speed_txt"] = ""
            job["eta"] = ""
            job["status"] = status
            self._ui_q.put(("done", job))

    def _on_job_done(self, job):
        """Bir iş bittiğinde (ana iş parçacığı): çıktıya yaz, tepsideyse bildirim gönder."""
        jid = job["id"]
        title = short(job["title"], 60)
        if job["status"] == ST_DONE:
            self._write_log(f"✅ [#{jid}] Completed: {job['title']}\n", "ok")
            self._notify(f"✅ {title}")
        elif job["status"] == ST_CANCEL:
            self._write_log(f"⏹ [#{jid}] Cancelled\n", "inf")
        else:
            why = f": {job['msg']}" if job["msg"] else ""
            self._write_log(f"❌ [#{jid}] Failed{why}\n", "err")
            self._notify(f"❌ {title}")

    # ── Kuyruk listesi ─────────────────────────────────────────
    def _row_values(self, j):
        st = j["status"]
        if st == ST_RUN:
            status = j["note"] or "downloading"
            if j["stage"] > 1 and not j["note"]:
                status += f" #{j['stage']}"
        else:
            status = st
        if st in (ST_RUN, ST_MERGE, ST_DONE):
            prog = bar_text(j["percent"])
        elif st in (ST_ERR, ST_CANCEL) and j["percent"] > 0:
            prog = bar_text(j["percent"])
        else:
            prog = ""
        return (f"#{j['id']}  {short(j['title'], 24)}", status, prog,
                j["speed_txt"], j["eta"])

    def _refresh_tree(self):
        """Listeyi iş durumlarıyla eşitler (seçimi bozmadan)."""
        want = {str(j["id"]) for j in self._jobs_list}
        for iid in self.tree.get_children():
            if iid not in want:
                self.tree.delete(iid)
        existing = set(self.tree.get_children())
        for j in self._jobs_list:
            iid = str(j["id"])
            vals = self._row_values(j)
            tags = (STATUS_TAG[j["status"]],)
            if iid in existing:
                self.tree.item(iid, values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=iid, values=vals, tags=tags)
                self.tree.see(iid)

        running = [j for j in self._jobs_list if j["status"] in (ST_RUN, ST_MERGE)]
        waiting = sum(1 for j in self._jobs_list if j["status"] == ST_WAIT)
        finished = sum(1 for j in self._jobs_list
                       if j["status"] in (ST_DONE, ST_ERR, ST_CANCEL))
        parts = []
        if running:
            parts.append(f"{len(running)} downloading")
        if waiting:
            parts.append(f"{waiting} waiting")
        if finished:
            parts.append(f"{finished} finished")
        self.lbl_queue.config(text=" · ".join(parts) if parts else "Queue empty")

        # Görev çubuğunda küçültülmüş hâlde de ilerleme görünsün
        if running:
            t = f"UHF Downloader — {running[0]['percent']:.0f}%"
            if waiting:
                t += f" (+{waiting} waiting)"
            self.title(t)
        else:
            self.title("UHF Downloader")

    def _selected_jobs(self):
        sel = set(self.tree.selection())
        return [j for j in self._jobs_list if str(j["id"]) in sel]

    def _cancel_job(self, j):
        if j["status"] == ST_WAIT:
            j["status"] = ST_CANCEL          # işçi sıradan alınca atlar
        elif j["status"] in (ST_RUN, ST_MERGE):
            j["cancel"] = True
            if j.get("proc"):
                kill_tree(j["proc"])

    def _cancel_selected(self):
        jobs = self._selected_jobs()
        if not jobs:
            self._write_log("ℹ Select a row to cancel first.\n", "inf")
            return
        for j in jobs:
            self._cancel_job(j)
        self._refresh_tree()

    def _retry_selected(self):
        jobs = [j for j in self._selected_jobs() if j["status"] in (ST_ERR, ST_CANCEL)]
        if not jobs:
            self._write_log("ℹ Select a failed or cancelled row to retry.\n", "inf")
            return
        for j in jobs:
            j.update(status=ST_WAIT, percent=0.0, speed_txt="", eta="",
                     msg="", note="", stage=0, cancel=False)
            self._pending.put(j)
            self._write_log(f"↻ [#{j['id']}] Re-queued\n", "inf")
        self._refresh_tree()

    def _remove_jobs(self, jobs):
        busy = 0
        for j in jobs:
            if j["status"] in (ST_RUN, ST_MERGE):
                self._cancel_job(j)          # çalışan iş önce iptal edilir, sonra kaldırılır
                busy += 1
                continue
            if j["status"] == ST_WAIT:
                j["status"] = ST_CANCEL      # sıradaki kopyası işçi tarafından atlanır
            if j in self._jobs_list:
                self._jobs_list.remove(j)
        if busy:
            self._write_log("ℹ Running download cancelled; press Remove again once it stops.\n", "inf")
        self._refresh_tree()

    def _remove_selected(self):
        jobs = self._selected_jobs()
        if jobs:
            self._remove_jobs(jobs)

    def _clear_finished(self):
        self._remove_jobs([j for j in self._jobs_list
                           if j["status"] in (ST_DONE, ST_ERR, ST_CANCEL)])

    def _open_selected_folder(self):
        jobs = self._selected_jobs()
        d = jobs[0]["outdir"] if jobs else self.download_dir.get()
        try:
            if os.name == "nt":
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            self._write_log(f"⚠ Could not open folder: {e}\n", "err")

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        try:
            self.qmenu.tk_popup(event.x_root, event.y_root)
        finally:
            self.qmenu.grab_release()


if __name__ == "__main__":
    # Program yanlışlıkla kendi alt süreci olarak başlatıldıysa pencere açma
    if os.environ.get(GUARD_VAR):
        sys.exit(3)
    app = App()
    app.mainloop()
