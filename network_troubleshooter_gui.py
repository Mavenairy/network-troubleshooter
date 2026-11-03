"""
Network Troubleshooter - Rule-Based Expert System with GUI (automatic diagnosis)
Components: Fact Collector (real tests run on this PC) + Knowledge Base (rules)
            + Inference Engine (forward chaining) + Explanation + Auto Fix + GUI (Tkinter)
No questions are asked: the program measures the facts itself.
Requires: Python 3 (Tkinter included). No extra packages.
Run: python network_troubleshooter_gui.py
"""
import ctypes
import datetime
import json
import os
import platform
import queue
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk, filedialog, messagebox

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

# =====================================================================
# 1) FACT COLLECTOR - runs real tests on this computer
# =====================================================================
TESTS = ["Wi-Fi adapter & radio", "Network adapter & IP", "Default gateway", "Router (gateway) reachable",
         "Internet & connection quality", "DNS resolution", "Web access (HTTPS)",
         "Wi-Fi signal", "Internet speed"]


def run_cmd(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="ignore",
                              timeout=timeout, creationflags=NO_WINDOW).stdout
    except Exception:
        return ""


def get_local_ip():
    """Local IP used to reach the internet (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def get_gateway():
    """Default gateway (language-independent parsing)."""
    if IS_WIN:
        m = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                      run_cmd(["route", "print", "0.0.0.0"]))
    elif IS_MAC:
        m = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", run_cmd(["route", "-n", "get", "default"]))
    else:
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", run_cmd(["ip", "route"]))
    return m.group(1) if m else None


def ping(host, count=1):
    """Returns (replies_received, list_of_times_ms)."""
    args = (["ping", "-n", str(count), "-w", "2000", host] if IS_WIN
            else ["ping", "-c", str(count), "-W", "2", host])
    out = run_cmd(args, timeout=count * 3 + 4)
    received = len(re.findall(r"ttl=", out, re.I))
    times = [float(t) for t in re.findall(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)]
    if received and not times:      # localized output: assume fast rather than slow
        times = [0.0]
    return received, times


def dns_works():
    try:
        socket.gethostbyname("www.google.com")
        return True
    except OSError:
        return False


def web_works():
    for url in ("https://www.google.com", "https://www.microsoft.com"):
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                if r.status < 400:
                    return True
        except Exception:
            pass
    return False


def wifi_info():
    """(network name, signal %) on Windows Wi-Fi; (None, None) otherwise."""
    if not IS_WIN:
        return None, None
    out = run_cmd(["netsh", "wlan", "show", "interfaces"])
    sig = re.search(r"Signal\s*:\s*(\d+)\s*%", out)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M)
    return (ssid.group(1) if ssid else None), (int(sig.group(1)) if sig else None)


# PowerShell (WinRT) snippet that finds the Wi-Fi radio in $wifi
WINRT_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$wifi = $radios | Where-Object { $_.Kind -eq 'WiFi' } | Select-Object -First 1
"""

WIFI_DETECT_PS = ("$ErrorActionPreference='SilentlyContinue'\n"
                  "Get-NetAdapter | Where-Object { $_.PhysicalMediaType -like '*802.11*' } | "
                  "ForEach-Object { 'A|' + $_.Name + '|' + $_.Status }\n"
                  + WINRT_PS + "if ($wifi) { 'R|' + $wifi.State }")


def wifi_state():
    """Windows: is the Wi-Fi adapter disabled or the Wi-Fi radio switched off?
    Returns None when there is no Wi-Fi adapter or the system is not Windows."""
    if not IS_WIN:
        return None
    out = run_cmd(["powershell", "-NoProfile", "-Command", WIFI_DETECT_PS], timeout=25)
    adapters = [l.strip().split("|") for l in out.splitlines() if l.startswith("A|")]
    radio = next((l.strip().split("|")[1] for l in out.splitlines() if l.startswith("R|")), None)
    if not adapters and radio is None:
        return None
    return {"adapter_disabled": any(len(a) > 2 and a[2] == "Disabled" for a in adapters),
            "radio_off": (radio == "Off") if radio else None}


SPEED_DOWN = "https://speed.cloudflare.com/__down?bytes=25000000"
SPEED_UP = "https://speed.cloudflare.com/__up"
UA = {"User-Agent": "Mozilla/5.0"}


def _run_threads(target, count, secs):
    """Runs `count` workers for about `secs` seconds. Returns Mbps (or None)."""
    total, lock = [0], threading.Lock()
    start = time.time()
    deadline = start + secs
    threads = [threading.Thread(target=target, args=(deadline, total, lock), daemon=True)
               for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(secs + 6)
    elapsed = max(time.time() - start, 0.5)
    return total[0] * 8 / elapsed / 1e6 if total[0] else None


def download_speed(secs=6):
    def work(deadline, total, lock):
        while time.time() < deadline:
            try:
                req = urllib.request.Request(SPEED_DOWN, headers=UA)
                with urllib.request.urlopen(req, timeout=8) as r:
                    while time.time() < deadline:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        with lock:
                            total[0] += len(chunk)
            except Exception:
                return
    return _run_threads(work, 4, secs)


def upload_speed(secs=4):
    data = bytes(1_000_000)

    def work(deadline, total, lock):
        while time.time() < deadline:
            try:
                req = urllib.request.Request(SPEED_UP, data=data, method="POST",
                                             headers={**UA, "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                with lock:
                    total[0] += len(data)
            except Exception:
                return
    return _run_threads(work, 3, secs)


def speed_rating(mbps):
    for limit, word in ((50, "Excellent"), (25, "Very good"), (10, "Good"), (5, "Fair")):
        if mbps >= limit:
            return word
    return "Slow"


def diagnose(report):
    """Runs all tests, reports progress, returns the dictionary of facts."""
    facts = {}

    report(0, "run", "Checking Wi-Fi...")
    wifi = wifi_state()
    if wifi is None:
        report(0, "skip", "No Wi-Fi adapter detected")
    else:
        facts["wifi_adapter_disabled"] = wifi["adapter_disabled"]
        facts["wifi_radio_off"] = wifi["radio_off"]
        if wifi["adapter_disabled"]:
            report(0, "fail", "Wi-Fi adapter is disabled")
        elif wifi["radio_off"]:
            report(0, "fail", "Wi-Fi radio is turned off")
        else:
            report(0, "ok", "Wi-Fi is turned on")

    report(1, "run", "Checking...")
    ip = get_local_ip()
    facts["no_link"] = ip is None
    facts["has_ip"] = bool(ip) and not ip.startswith(("169.254.", "0."))
    if ip is None:
        report(1, "fail", "No active network connection")
    elif not facts["has_ip"]:
        report(1, "fail", f"Invalid address {ip} (no DHCP lease)")
    else:
        facts["local_ip"] = ip
        report(1, "ok", f"Local IP {ip}")
    if not facts["has_ip"]:
        for i in range(2, len(TESTS)):
            report(i, "skip", "Skipped")
        return facts

    report(2, "run", "Checking...")
    gw = get_gateway()
    facts["gateway_found"] = gw is not None
    facts["gateway"] = gw
    report(2, "ok" if gw else "fail", f"Gateway {gw}" if gw else "No default gateway set")

    report(3, "run", "Pinging router...")
    if gw:
        rec, times = ping(gw, 2)
        facts["ping_gateway"] = rec > 0
        report(3, "ok" if rec else "fail",
               f"Reply in {statistics.mean(times):.0f} ms" if rec else "No reply from router")
    else:
        report(3, "skip", "No gateway to test")

    report(4, "run", "Pinging 8.8.8.8 (5 packets)...")
    count = 5
    rec, times = ping("8.8.8.8", count)
    if rec == 0:
        count = 2
        rec, times = ping("1.1.1.1", count)
    facts["ping_ip"] = rec > 0
    if rec:
        avg = statistics.mean(times)
        facts["packet_loss"] = rec < count
        facts["latency_ms"] = avg
        facts["high_latency"] = avg > 150
        bad = facts["packet_loss"] or facts["high_latency"]
        report(4, "warn" if bad else "ok", f"{rec}/{count} replies, average {avg:.0f} ms")
    else:
        report(4, "fail", "No reply from the internet")

    report(5, "run", "Resolving www.google.com...")
    facts["dns_ok"] = dns_works()
    report(5, "ok" if facts["dns_ok"] else "fail",
           "Name resolved" if facts["dns_ok"] else "Cannot resolve domain names")

    report(6, "run", "Opening a website...")
    facts["http_ok"] = web_works()
    report(6, "ok" if facts["http_ok"] else "fail",
           "Websites reachable" if facts["http_ok"] else "Cannot open websites")

    ssid, sig = wifi_info()
    facts["ssid"] = ssid
    if sig is None:
        report(7, "skip", "Not on Wi-Fi / not available")
    else:
        facts["wifi_weak"] = sig < 50
        report(7, "warn" if sig < 50 else "ok", f"Signal {sig}%")

    if not facts.get("ping_ip"):
        report(8, "skip", "No internet connection")
        return facts
    report(8, "run", "Measuring download...")
    down = download_speed()
    if down is None:
        report(8, "warn", "Speed test server unreachable")
        return facts
    facts["speed_down"] = down
    facts["speed_low"] = down < 5
    facts["speed_very_low"] = down < 1
    report(8, "run", f"Download {down:.1f} Mbps - measuring upload...")
    up = upload_speed()
    facts["speed_up"] = up
    text = f"↓ {down:.1f}   ↑ {up:.1f} Mbps" if up else f"↓ {down:.1f} Mbps"
    report(8, "warn" if down < 5 else "ok", f"{text}  ·  {speed_rating(down)}")
    return facts


# =====================================================================
# 2) KNOWLEDGE BASE - IF-THEN rules
#    (name, conditions, conclusion, severity, advice)
#    A condition value can be True / False / a tuple of allowed values.
# =====================================================================
RULES = [
    ("R0a", {"no_link": True, "wifi_adapter_disabled": True}, "wifi_disabled", "error",
     "The Wi-Fi adapter is disabled. Enable it in Network Connections (or use Auto Fix)."),
    ("R0b", {"no_link": True, "wifi_adapter_disabled": (False, None), "wifi_radio_off": True},
     "wifi_radio_off", "error",
     "Wi-Fi is switched off (Airplane mode or the Wi-Fi toggle). Turn it on (or use Auto Fix)."),
    ("R1", {"no_link": True, "wifi_adapter_disabled": (False, None), "wifi_radio_off": (False, None)},
     "no_connection", "error",
     "No active connection. Plug in the cable, turn Wi-Fi on, and turn Airplane mode off."),
    ("R2", {"no_link": False, "has_ip": False}, "dhcp_failure", "error",
     "The PC did not get an IP address. Restart the router, then run: ipconfig /release and ipconfig /renew."),
    ("R3", {"has_ip": True, "gateway_found": False, "ping_ip": False}, "no_gateway", "error",
     "No default gateway is configured. Set the gateway manually or switch the adapter to automatic (DHCP)."),
    ("R4", {"gateway_found": True, "ping_gateway": False, "ping_ip": False}, "gateway_unreachable", "error",
     "The router does not answer. Check router power and cables, then restart it."),
    ("R5", {"ping_gateway": True, "ping_ip": False}, "internet_unreachable", "error",
     "Your router works but the internet is down. Restart modem/router and contact your ISP."),
    ("R6", {"ping_ip": True, "dns_ok": False}, "dns_failure", "error",
     "DNS is failing. Set DNS to 8.8.8.8 / 1.1.1.1 and run: ipconfig /flushdns."),
    ("R7", {"ping_ip": True, "dns_ok": True, "http_ok": False}, "web_blocked", "error",
     "Internet is reachable but web access fails. Check proxy, VPN, firewall or antivirus web protection."),
    ("R8", {"ping_ip": True, "dns_ok": True, "http_ok": True}, "internet_ok", "fact", None),
    ("R9", {"packet_loss": True, "wifi_weak": True}, "wifi_interference", "warn",
     "Packet loss on a weak Wi-Fi signal. Move closer, use the 5 GHz band, or change the Wi-Fi channel."),
    ("R10", {"packet_loss": True, "wifi_weak": (False, None)}, "unstable_link", "warn",
     "Packets are being lost. Check for loose or damaged cables and restart the router."),
    ("R11", {"high_latency": True, "wifi_weak": True}, "weak_wifi", "warn",
     "Slow responses and a weak Wi-Fi signal. Move closer to the router or add an extender."),
    ("R12", {"high_latency": True, "wifi_weak": (False, None)}, "congestion", "warn",
     "High latency. The network may be busy: stop big downloads/streams, or check with your ISP."),
    ("R12a", {"speed_very_low": True}, "very_slow_internet", "warn",
     "Download speed is under 1 Mbps. Move closer to the router, stop other downloads, restart the router, "
     "and contact your ISP if it stays this low."),
    ("R12b", {"speed_low": True, "speed_very_low": False}, "slow_internet", "warn",
     "Download speed is under 5 Mbps. Check what else is using the network, restart the router, "
     "and compare with your ISP plan."),
    ("R13", {"internet_ok": True, "packet_loss": False, "high_latency": False,
             "speed_low": (False, None)}, "healthy", "ok",
     "Everything is working normally. No action needed."),
]

FACT_LABELS = {
    "local_ip": "Local IP address", "gateway": "Default gateway", "ssid": "Wi-Fi network",
    "latency_ms": "Average latency (ms)", "speed_down": "Download (Mbps)", "speed_up": "Upload (Mbps)",
    "speed_low": "Download speed is low (under 5 Mbps)", "speed_very_low": "Download speed is very low (under 1 Mbps)",
    "wifi_adapter_disabled": "Wi-Fi adapter is disabled", "wifi_radio_off": "Wi-Fi radio is off",
    "no_link": "No active network connection", "has_ip": "Valid IP address",
    "gateway_found": "Default gateway exists", "ping_gateway": "Router replies to ping",
    "ping_ip": "Internet replies to ping", "dns_ok": "DNS works",
    "http_ok": "Websites open", "packet_loss": "Packets are lost",
    "high_latency": "Latency is high (>150 ms)", "wifi_weak": "Wi-Fi signal is weak",
    "internet_ok": "Internet works (ping + DNS + web)",
}


# =====================================================================
# 3) INFERENCE ENGINE - forward chaining
# =====================================================================
def holds(cond, facts):
    for fact, wanted in cond.items():
        allowed = wanted if isinstance(wanted, tuple) else (wanted,)
        if facts.get(fact) not in allowed:
            return False
    return True


def infer(facts):
    """Fire rules repeatedly until no new conclusion is reached."""
    facts, fired, results = dict(facts), set(), []
    changed = True
    while changed:
        changed = False
        for name, cond, conclusion, severity, advice in RULES:
            if name not in fired and holds(cond, facts):
                fired.add(name)
                facts[conclusion] = True
                results.append((name, cond, conclusion, severity, advice))
                changed = True
    return results


# =====================================================================
# 4) EXPLANATION FACILITY
# =====================================================================
def why_text(cond):
    parts = []
    for fact, wanted in cond.items():
        yes = wanted is True
        parts.append(f"{FACT_LABELS.get(fact, fact)}: {'yes' if yes else 'no'}")
    return "; ".join(parts)


TITLES = {"wifi_disabled": "Wi-Fi Adapter Is Disabled", "wifi_radio_off": "Wi-Fi Is Turned Off",
          "dns_failure": "DNS Failure", "very_slow_internet": "Very Slow Internet", "slow_internet": "Slow Internet", "weak_wifi": "Weak Wi-Fi Signal", "wifi_interference": "Wi-Fi Interference", "dhcp_failure": "DHCP Failure"}


def title_of(conclusion):
    return TITLES.get(conclusion, conclusion.replace("_", " ").title())


# =====================================================================
# 5) AUTO FIX - maps each diagnosis to repair commands (Windows)
#    Needs administrator rights; the app offers to restart itself as admin.
# =====================================================================
def ps(script):
    return ["powershell", "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; " + script]


UP = "Get-NetAdapter | Where-Object Status -eq 'Up'"
RELEASE = ("Release IP address", ["ipconfig", "/release"])
RENEW = ("Renew IP address", ["ipconfig", "/renew"])
FLUSH = ("Flush DNS cache", ["ipconfig", "/flushdns"])
SET_DNS = ("Set DNS to 8.8.8.8 / 1.1.1.1", ps(
    UP + " | ForEach-Object { Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex "
         "-ServerAddresses ('8.8.8.8','1.1.1.1') }"))
ENABLE = ("Enable disabled network adapters", ps(
    "Get-NetAdapter | Where-Object Status -eq 'Disabled' | Enable-NetAdapter -Confirm:$false"))
RESTART = ("Restart network adapter", ps(UP + " | Restart-NetAdapter -Confirm:$false"))
PROXY_OFF = ("Turn off system proxy", [
    "reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings",
    "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "0", "/f"])
WINHTTP = ("Reset WinHTTP proxy", ["netsh", "winhttp", "reset", "proxy"])
ENABLE_WIFI = ("Turn on Wi-Fi adapter", ps(
    "Get-NetAdapter | Where-Object { $_.PhysicalMediaType -like '*802.11*' -and $_.Status -eq 'Disabled' } "
    "| Enable-NetAdapter -Confirm:$false"))
RADIO_ON = ("Turn on Wi-Fi radio", ["powershell", "-NoProfile", "-Command", WINRT_PS +
    "if (-not $wifi) { throw 'No Wi-Fi radio found' }; "
    "Await ($wifi.SetStateAsync('On')) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null"])

# diagnosis -> repair steps (problems with no entry need a manual fix)
FIXES = {
    "wifi_disabled": [ENABLE_WIFI, RENEW],
    "wifi_radio_off": [ENABLE_WIFI, RADIO_ON, RENEW],
    "no_connection": [ENABLE, RELEASE, RENEW, FLUSH],
    "dhcp_failure": [RELEASE, RENEW, FLUSH],
    "no_gateway": [RELEASE, RENEW],
    "gateway_unreachable": [RESTART, RELEASE, RENEW],
    "internet_unreachable": [RELEASE, RENEW, FLUSH],
    "dns_failure": [FLUSH, SET_DNS],
    "web_blocked": [PROXY_OFF, WINHTTP, FLUSH],
    "unstable_link": [RESTART],
}


def collect_steps(results):
    steps = []
    for _, _, conclusion, _, _ in results:
        for step in FIXES.get(conclusion, []):
            if step not in steps:
                steps.append(step)
    return steps


def run_step(args):
    """Runs one repair command. Returns (success, short message)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="ignore",
                           timeout=90, creationflags=NO_WINDOW)
        lines = [l.strip() for l in (p.stdout + p.stderr).splitlines() if l.strip()]
        return p.returncode == 0, (lines[-1][:90] if lines else "")
    except Exception as e:
        return False, str(e)[:90]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_admin():
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                f'"{os.path.abspath(__file__)}"', None, 1)
        return r > 32
    except Exception:
        return False


# =====================================================================
# 6) HEALTH SCORE, HISTORY, RULE TEXT
# =====================================================================
def health_score(facts, results):
    """0-100 score from the diagnoses (errors cost more than warnings)."""
    if facts.get("no_link") or facts.get("has_ip") is False:
        return 0
    score = 100
    for _, _, _, severity, _ in results:
        score -= {"error": 45, "warn": 15}.get(severity, 0)
    if facts.get("ping_ip") is False:
        score = min(score, 20)
    return max(0, score)


HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".network_troubleshooter_history.json")


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(entry):
    data = (load_history() + [entry])[-50:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def clear_history():
    try:
        os.remove(HISTORY_FILE)
    except OSError:
        pass


def rule_text(cond):
    return " AND ".join(FACT_LABELS.get(f, f.replace("_", " ")) if w is True
                        else "NOT (" + FACT_LABELS.get(f, f.replace("_", " ")) + ")"
                        for f, w in cond.items())


# =====================================================================
# 7) GUI  (sidebar + 3 pages: Dashboard, Expert System, History)
# =====================================================================
FONT = "Segoe UI" if IS_WIN else "Helvetica"
BG, SIDE, CARD, CARD2 = "#0b1120", "#0d1527", "#121a2e", "#1a2540"
BORDER, TRACK = "#222f4a", "#26344f"
TEXT, MUTED = "#e6edf7", "#8794ad"
ACCENT, GREEN, RED, AMBER = "#4f9cf9", "#34d399", "#f87171", "#fbbf24"
SEV_COLOR = {"error": RED, "warn": AMBER, "ok": GREEN}
SEV_ORDER = {"error": 0, "warn": 1, "ok": 2}
MODE_COLOR = {"idle": MUTED, "busy": ACCENT, "ok": GREEN, "warn": AMBER, "error": RED}
MODE_SYMBOL = {"idle": "•", "ok": "✔", "warn": "!", "error": "✖"}
SCALE = 1.0  # set from the screen DPI when the window opens


def px(n):
    return int(n * SCALE)


def _rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def lerp(c1, c2, t):
    """Colour between c1 and c2 (t=0 -> c1, t=1 -> c2)."""
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def close_enough(c1, c2):
    return max(abs(x - y) for x, y in zip(_rgb(c1), _rgb(c2))) < 6


def score_color(score):
    return GREEN if score >= 80 else AMBER if score >= 50 else RED


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class FadeMixin:
    """Smooth fill-colour animation for canvas widgets that own self.shape."""

    def _to(self, color):
        self.goal = color
        if not self.animating:
            self.animating = True
            self._step()

    def _step(self):
        self.cur = lerp(self.cur, self.goal, .35)
        if close_enough(self.cur, self.goal):
            self.cur, self.animating = self.goal, False
        else:
            self.after(16, self._step)
        self.itemconfigure(self.shape, fill=self.cur)


class Card(tk.Canvas):
    """Rounded panel. Put your widgets in card.inner."""

    def __init__(self, parent, fill=CARD, radius=20, pad=18, **kw):
        kw.setdefault("width", px(10))   # real size comes from the layout, not the default
        kw.setdefault("height", px(10))
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, **kw)
        self.fill, self.radius, self.pad = fill, px(radius), px(pad)
        self.inner = tk.Frame(self, bg=fill)
        self.win = self.create_window(0, 0, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._draw)

    def _draw(self, e):
        self.delete("bg")
        shape = rounded_rect(self, 1, 1, e.width - 1, e.height - 1, self.radius,
                             fill=self.fill, outline=BORDER, tags="bg")
        self.tag_lower(shape)
        self.coords(self.win, self.pad, self.pad)
        self.itemconfigure(self.win, width=max(1, e.width - 2 * self.pad),
                           height=max(1, e.height - 2 * self.pad))


class RoundButton(FadeMixin, tk.Canvas):
    """Pill button with smooth hover / press animation."""

    def __init__(self, parent, text, command, color, fg=BG, w=170, h=42, size=11):
        self.w, self.h = px(w), px(h)
        super().__init__(parent, width=self.w, height=self.h, bg=parent.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.command, self.base, self.fg = command, color, fg
        self.enabled, self.cur, self.goal, self.animating = True, color, color, False
        self.shape = rounded_rect(self, 1, 1, self.w - 1, self.h - 1, self.h // 2, fill=color, outline="")
        self.label = self.create_text(self.w / 2, self.h / 2, text=text, fill=fg, font=(FONT, size, "bold"))
        self.bind("<Enter>", lambda e: self.enabled and self._to(lerp(self.base, "#ffffff", .18)))
        self.bind("<Leave>", lambda e: self.enabled and self._to(self.base))
        self.bind("<ButtonPress-1>", lambda e: self.enabled and self._to(lerp(self.base, "#000000", .2)))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, e):
        if self.enabled and 0 <= e.x <= self.w and 0 <= e.y <= self.h:
            self._to(lerp(self.base, "#ffffff", .18))
            self.command()

    def set_enabled(self, on):
        self.enabled = on
        self.config(cursor="hand2" if on else "arrow")
        self.itemconfigure(self.label, fill=self.fg if on else MUTED)
        self._to(self.base if on else CARD2)

    def set_text(self, text):
        self.itemconfigure(self.label, text=text)

    def set_color(self, color):
        self.base = color
        self._to(color if self.enabled else CARD2)


class NavItem(FadeMixin, tk.Canvas):
    """Sidebar entry with hover and active highlight."""

    def __init__(self, parent, text, command):
        self.w, self.h = px(184), px(42)
        super().__init__(parent, width=self.w, height=self.h, bg=parent.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.command, self.active = command, False
        self.cur = self.goal = SIDE
        self.animating = False
        self.shape = rounded_rect(self, 1, 1, self.w - 1, self.h - 1, px(12), fill=SIDE, outline="")
        self.dot = self.create_oval(px(16), self.h / 2 - px(3), px(22), self.h / 2 + px(3), fill=TRACK, outline="")
        self.label = self.create_text(px(36), self.h / 2, text=text, anchor="w", fill=MUTED,
                                      font=(FONT, 10, "bold"))
        self.bind("<Enter>", lambda e: not self.active and self._to(CARD2))
        self.bind("<Leave>", lambda e: self._to(self._rest()))
        self.bind("<ButtonRelease-1>", lambda e: self.command())

    def _rest(self):
        return lerp(SIDE, ACCENT, .16) if self.active else SIDE

    def set_active(self, on):
        self.active = on
        self.itemconfigure(self.label, fill=ACCENT if on else MUTED)
        self.itemconfigure(self.dot, fill=ACCENT if on else TRACK)
        self._to(self._rest())


class StatTile(Card):
    """Small rounded tile: caption, big value, note."""

    def __init__(self, parent, caption):
        super().__init__(parent, CARD, 16, 14, height=px(92))
        tk.Label(self.inner, text=caption.upper(), bg=CARD, fg=MUTED, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x")
        self.value = tk.Label(self.inner, text="—", bg=CARD, fg=TEXT, font=(FONT, 16, "bold"), anchor="w")
        self.value.pack(fill="x")
        self.note = tk.Label(self.inner, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w")
        self.note.pack(fill="x")

    def set(self, value, note="", color=TEXT):
        self.value.config(text=value, fg=color)
        self.note.config(text=note)


class TestRow(tk.Frame):
    """One line of the test list with an animated status icon."""
    DETAIL = {"pending": MUTED, "run": ACCENT, "ok": MUTED, "warn": AMBER, "fail": RED, "skip": MUTED}

    def __init__(self, parent, name):
        super().__init__(parent, bg=CARD)
        self.sz, self.status, self.angle = px(30), "pending", 0
        self.icon = tk.Canvas(self, width=self.sz, height=self.sz, bg=CARD, highlightthickness=0)
        self.icon.pack(side="left", padx=(0, px(12)))
        col = tk.Frame(self, bg=CARD)
        col.pack(side="left", fill="x", expand=True)
        tk.Label(col, text=name, bg=CARD, fg=TEXT, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
        self.detail = tk.Label(col, text="Waiting", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w")
        self.detail.pack(fill="x")
        self.draw()

    def set(self, status, detail):
        self.status = status
        self.detail.config(text=detail, fg=self.DETAIL[status])
        self.draw()

    def spin(self):
        if self.status == "run":
            self.angle = (self.angle + 9) % 360
            self.draw()

    def draw(self):
        c, s, st, m = self.icon, self.sz, self.status, px(3)
        c.delete("all")
        box = (m, m, s - m, s - m)
        if st == "run":
            c.create_oval(*box, outline=TRACK, width=px(3))
            c.create_arc(*box, start=90 - self.angle, extent=-100, style="arc", outline=ACCENT, width=px(3))
        elif st in ("ok", "warn", "fail"):
            col = {"ok": GREEN, "warn": AMBER, "fail": RED}[st]
            c.create_oval(*box, fill=lerp(CARD, col, .18), outline=col, width=px(2))
            c.create_text(s / 2, s / 2, text={"ok": "✔", "warn": "!", "fail": "✖"}[st],
                          fill=col, font=(FONT, 10, "bold"))
        else:
            c.create_oval(*box, outline=TRACK, width=px(2))
            if st == "skip":
                c.create_text(s / 2, s / 2, text="–", fill=MUTED, font=(FONT, 10, "bold"))


class ScrollArea(tk.Frame):
    """Scrollable container with a slim dark scrollbar and mouse-wheel support."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.on_width = None
        self.inner.bind("<Configure>", lambda e: self.update_region())
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)

    def _resize(self, e):
        self.canvas.itemconfigure(self.win, width=e.width)
        self.update_region()
        if self.on_width:
            self.on_width(e.width)

    def update_region(self):
        h = max(self.inner.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), h))

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)

    def width(self):
        return max(220, self.canvas.winfo_width() - px(100))

    def _enter(self, _):
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _leave(self, _):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(ev)


def placeholder(area, text):
    tk.Label(area.inner, text=text, bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill="x", pady=px(6))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global SCALE
        SCALE = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("Network Troubleshooter - Expert System")
        self.geometry(f"{px(1200)}x{px(820)}")
        self.minsize(px(1100), px(780))
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.running = self.busy = self.fixing = False
        self.fix_total = 0
        self.results, self.steps, self.fix_log, self.wrap_labels, self.kb_wrap = [], [], [], [], []
        self.facts, self.facts_all, self.fired, self.score = {}, {}, set(), None
        self.test_state = [("pending", "")] * len(TESTS)
        self.mode, self.mode_col, self.cur_col = "idle", MUTED, MUTED
        self.ring_val, self.spin = 0.0, 0.0
        self.page = "dashboard"
        self.build()
        self.bind("<F5>", lambda e: self.start())
        self.bind("<Control-e>", lambda e: self.export())
        self.after(100, self.poll)
        self.after(16, self.tick)
        self.after(600, self.start)  # diagnose automatically on launch

    # ================= layout =================
    def build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.Vertical.TScrollbar", troughcolor=CARD, background=TRACK, bordercolor=CARD,
                        lightcolor=TRACK, darkcolor=TRACK, arrowcolor=CARD, gripcount=0, relief="flat")
        style.map("Dark.Vertical.TScrollbar", background=[("active", MUTED)])

        side = tk.Frame(self, bg=SIDE, width=px(214))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.build_sidebar(side)

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True, padx=px(28), pady=px(22))
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.pages = {}
        for name, builder in (("dashboard", self.build_dashboard), ("expert", self.build_expert),
                              ("history", self.build_history)):
            page = tk.Frame(self.content, bg=BG)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
            builder(page)
        self.show("dashboard")

    def build_sidebar(self, side):
        top = tk.Frame(side, bg=SIDE)
        top.pack(fill="x", padx=px(14), pady=(px(24), px(22)))
        s = px(40)
        logo = tk.Canvas(top, width=s, height=s, bg=SIDE, highlightthickness=0)
        logo.pack(side="left", padx=(px(4), px(10)))
        rounded_rect(logo, 0, 0, s, s, px(12), fill=ACCENT, outline="")
        cx, cy = s / 2, s * 0.68
        for r in (px(14), px(10), px(6)):
            logo.create_arc(cx - r, cy - r, cx + r, cy + r, start=45, extent=90, style="arc",
                            outline=SIDE, width=px(3))
        logo.create_oval(cx - px(2), cy - px(2), cx + px(2), cy + px(2), fill=SIDE, outline=SIDE)
        tk.Label(top, text="Network\nTroubleshooter", bg=SIDE, fg=TEXT, font=(FONT, 11, "bold"),
                 justify="left").pack(side="left")
        self.nav = {}
        for name, text in (("dashboard", "Dashboard"), ("expert", "Expert System"), ("history", "History")):
            item = NavItem(side, text, lambda n=name: self.show(n))
            item.pack(padx=px(14), pady=px(3))
            self.nav[name] = item
        tk.Label(side, text="Rule-based expert system\nForward chaining inference\n\nCopyright © 2026 Maven. \nAll rights reserved.\n\nF5  scan    Ctrl+E  export",
                 bg=SIDE, fg=MUTED, font=(FONT, 8), justify="left").pack(side="bottom", anchor="w",
                                                                         padx=px(20), pady=px(20))

    def show(self, name):
        self.page = name
        self.pages[name].tkraise()
        for n, item in self.nav.items():
            item.set_active(n == name)

    def page_header(self, page, title, sub):
        hdr = tk.Frame(page, bg=BG)
        hdr.pack(fill="x", pady=(0, px(14)))
        left = tk.Frame(hdr, bg=BG)
        left.pack(side="left")
        tk.Label(left, text=title, bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w")
        tk.Label(left, text=sub, bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        return hdr

    # ---------- dashboard ----------
    def build_dashboard(self, page):
        hdr = self.page_header(page, "Dashboard", "Automatic diagnosis of your network - no questions asked")
        RoundButton(hdr, "Export report", self.export, CARD2, fg=TEXT, w=124, h=32, size=9).pack(side="right")
        self.stamp = tk.Label(hdr, text="", bg=BG, fg=MUTED, font=(FONT, 9))
        self.stamp.pack(side="right", padx=px(14))

        hero = Card(page, CARD, 22, 20, height=px(144))
        hero.pack(fill="x", pady=(0, px(12)))
        hi = hero.inner
        hi.columnconfigure(1, weight=1)
        hi.rowconfigure(0, weight=1)
        rs, m = px(100), px(8)
        self.ring = tk.Canvas(hi, width=rs, height=rs, bg=CARD, highlightthickness=0)
        self.ring.grid(row=0, column=0, padx=(0, px(24)))
        self.ring.create_oval(m, m, rs - m, rs - m, outline=TRACK, width=px(8))
        self.arc = self.ring.create_arc(m, m, rs - m, rs - m, start=90, extent=-1, style="arc",
                                        outline=MUTED, width=px(8))
        self.ring_txt = self.ring.create_text(rs / 2, rs / 2, text="•", fill=MUTED, font=(FONT, 22, "bold"))
        txt = tk.Frame(hi, bg=CARD)
        txt.grid(row=0, column=1, sticky="ew")
        self.h_title = tk.Label(txt, text="Ready", bg=CARD, fg=MUTED, font=(FONT, 20, "bold"), anchor="w")
        self.h_title.pack(fill="x")
        self.h_sub = tk.Label(txt, text="Press Run Diagnosis to check your network.", bg=CARD, fg=MUTED,
                              font=(FONT, 10), anchor="w", justify="left", wraplength=px(480))
        self.h_sub.pack(fill="x", pady=(px(4), 0))
        bf = tk.Frame(hi, bg=CARD)
        bf.grid(row=0, column=2, padx=(px(16), 0))
        self.btn = RoundButton(bf, "Run Diagnosis", self.start, ACCENT)
        self.btn.pack()
        self.fix_btn = RoundButton(bf, "Auto Fix", self.auto_fix, GREEN)
        self.fix_btn.pack(pady=(px(10), 0))
        self.fix_btn.set_enabled(False)

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(0, px(12)))
        self.tiles = {}
        for i, key in enumerate(("down", "up", "latency", "network")):
            tile = StatTile(row, {"down": "Download", "up": "Upload", "latency": "Latency",
                                  "network": "Network"}[key])
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(6), 0 if i == 3 else px(6)))
            row.columnconfigure(i, weight=1, uniform="t")
            self.tiles[key] = tile

        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4, uniform="c")
        body.columnconfigure(1, weight=6, uniform="c")
        body.rowconfigure(0, weight=1)
        tcard = Card(body, CARD, 20, 16)
        tcard.grid(row=0, column=0, sticky="nsew", padx=(0, px(8)))
        tk.Label(tcard.inner, text="TESTS", bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, px(4)))
        self.rows = [TestRow(tcard.inner, n) for n in TESTS]
        for r in self.rows:
            r.pack(fill="x", pady=px(2))
        dcard = Card(body, CARD, 20, 16)
        dcard.grid(row=0, column=1, sticky="nsew", padx=(px(8), 0))
        tk.Label(dcard.inner, text="DIAGNOSIS", bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, px(4)))
        self.diag = ScrollArea(dcard.inner, CARD)
        self.diag.pack(fill="both", expand=True)
        self.diag.on_width = lambda w: self.rewrap(self.wrap_labels, w)
        self.write_output([])

    # ---------- expert system view ----------
    def build_expert(self, page):
        self.page_header(page, "Expert System", "Knowledge base, working memory and inference trace")
        arch = Card(page, CARD, 18, 14, height=px(62))
        arch.pack(fill="x", pady=(0, px(12)))
        tk.Label(arch.inner, bg=CARD, fg=TEXT, font=(FONT, 10, "bold"), anchor="w",
                 text=f"Fact collector ({len(TESTS)} live tests)   →   Inference engine (forward chaining, "
                      f"{len(RULES)} rules)   →   Explanation  +  Auto Fix").pack(fill="x", pady=px(4))
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4, uniform="k")
        body.columnconfigure(1, weight=6, uniform="k")
        body.rowconfigure(0, weight=1)
        for col, title in ((0, "FACTS  (working memory)"), (1, "RULES  (fired rules are highlighted)")):
            card = Card(body, CARD, 20, 16)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, px(8)) if col == 0 else (px(8), 0))
            tk.Label(card.inner, text=title, bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, px(4)))
            area = ScrollArea(card.inner, CARD)
            area.pack(fill="both", expand=True)
            setattr(self, "kb_facts" if col == 0 else "kb_rules", area)
        self.kb_rules.on_width = lambda w: self.rewrap(self.kb_wrap, w)
        self.render_expert()

    def render_expert(self):
        self.kb_facts.clear()
        self.kb_rules.clear()
        self.kb_wrap = []
        if not self.facts_all:
            placeholder(self.kb_facts, "Run a diagnosis to see the facts.")
        for key, val in self.facts_all.items():
            if val is None:
                continue
            row = tk.Frame(self.kb_facts.inner, bg=CARD2)
            row.pack(fill="x", pady=px(2), padx=(0, px(8)))
            label = FACT_LABELS.get(key) or title_of(key)
            tk.Label(row, text=label, bg=CARD2, fg=TEXT, font=(FONT, 9), anchor="w").pack(side="left", padx=px(10), pady=px(6))
            if val is True:
                text, col = "YES", GREEN
            elif val is False:
                text, col = "NO", MUTED
            else:
                text, col = (f"{val:.1f}" if isinstance(val, float) else str(val)), ACCENT
            tk.Label(row, text=text, bg=CARD2, fg=col, font=(FONT, 9, "bold")).pack(side="right", padx=px(10))
        for name, cond, conclusion, sev, advice in RULES:
            fired = name in self.fired
            color = SEV_COLOR.get(sev, ACCENT) if fired else TRACK
            box = tk.Frame(self.kb_rules.inner, bg=CARD2)
            box.pack(fill="x", pady=px(3), padx=(0, px(8)))
            tk.Frame(box, bg=color, width=px(4)).pack(side="left", fill="y")
            body = tk.Frame(box, bg=CARD2)
            body.pack(side="left", fill="x", expand=True, padx=px(12), pady=px(8))
            top = tk.Frame(body, bg=CARD2)
            top.pack(fill="x")
            tk.Label(top, text=name, bg=CARD2, fg=color if fired else MUTED, font=(FONT, 10, "bold")).pack(side="left")
            if fired:
                tk.Label(top, text=" FIRED ", bg=lerp(CARD2, color, .2), fg=color, font=(FONT, 8, "bold")).pack(side="right")
            then = title_of(conclusion) if sev != "fact" else "derive: " + FACT_LABELS.get(conclusion, conclusion)
            lbl = tk.Label(body, text=f"IF  {rule_text(cond)}\nTHEN  {then}", bg=CARD2,
                           fg=TEXT if fired else MUTED, font=(FONT, 9), anchor="w", justify="left",
                           wraplength=self.kb_rules.width())
            lbl.pack(fill="x", pady=(px(3), 0))
            self.kb_wrap.append(lbl)

    # ---------- history ----------
    def build_history(self, page):
        hdr = self.page_header(page, "History", "Your last scans (saved on this computer)")
        RoundButton(hdr, "Clear history", self.clear_hist, CARD2, fg=TEXT, w=130, h=34, size=9).pack(side="right")
        card = Card(page, CARD, 20, 16)
        card.pack(fill="both", expand=True)
        self.hist = ScrollArea(card.inner, CARD)
        self.hist.pack(fill="both", expand=True)
        self.render_history()

    def clear_hist(self):
        if messagebox.askyesno("Clear history", "Delete all saved scans?"):
            clear_history()
            self.render_history()

    def render_history(self):
        self.hist.clear()
        data = load_history()
        if not data:
            placeholder(self.hist, "No scans saved yet.")
            return
        for e in reversed(data):
            row = tk.Frame(self.hist.inner, bg=CARD2)
            row.pack(fill="x", pady=px(3), padx=(0, px(8)))
            score = e.get("score")
            col = score_color(score) if score is not None else MUTED
            s = px(46)
            badge = tk.Canvas(row, width=s, height=s, bg=CARD2, highlightthickness=0)
            badge.pack(side="left", padx=px(12), pady=px(10))
            badge.create_oval(px(3), px(3), s - px(3), s - px(3), outline=col, width=px(3))
            badge.create_text(s / 2, s / 2, text="-" if score is None else str(score), fill=col, font=(FONT, 11, "bold"))
            mid = tk.Frame(row, bg=CARD2)
            mid.pack(side="left", fill="x", expand=True)
            tk.Label(mid, text=e.get("title", "-"), bg=CARD2, fg=TEXT, font=(FONT, 11, "bold"), anchor="w").pack(fill="x")
            tk.Label(mid, text=e.get("time", ""), bg=CARD2, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
            parts = []
            if e.get("down"):
                parts.append(f"↓ {e['down']:.1f} Mbps")
            if e.get("up"):
                parts.append(f"↑ {e['up']:.1f}")
            if e.get("latency"):
                parts.append(f"{e['latency']:.0f} ms")
            tk.Label(row, text="   ".join(parts), bg=CARD2, fg=MUTED, font=(FONT, 9)).pack(side="right", padx=px(14))

    # ================= animation loop =================
    def progress(self):
        if self.fixing:
            return min(1.0, len(self.fix_log) / max(1, self.fix_total))
        done = sum(1 for st, _ in self.test_state if st in ("ok", "warn", "fail", "skip"))
        return done / len(TESTS)

    def tick(self):
        if self.cur_col != self.mode_col:
            self.cur_col = self.mode_col if close_enough(self.cur_col, self.mode_col) \
                else lerp(self.cur_col, self.mode_col, .18)
            self.h_title.config(fg=self.cur_col)
        if self.busy:
            target = 0.12 + 0.88 * self.progress()
        elif self.mode == "idle" or self.score is None:
            target = 0.0
        else:
            target = max(0.04, self.score / 100)
        self.ring_val += (target - self.ring_val) * .12
        if self.busy:
            self.spin = (self.spin + 7) % 360
        else:  # glide back to the top
            s = self.spin if self.spin <= 180 else self.spin - 360
            self.spin = (s * .8) % 360
        self.ring.itemconfigure(self.arc, start=90 - self.spin, extent=-max(1.0, 359.9 * self.ring_val),
                                outline=self.cur_col)
        if self.busy:
            label = f"{int(self.progress() * 100)}%"
        else:
            label = str(self.score) if self.score is not None else MODE_SYMBOL[self.mode]
        self.ring.itemconfigure(self.ring_txt, text=label, fill=self.cur_col)
        if self.page == "dashboard":
            for r in self.rows:
                r.spin()
        self.after(16, self.tick)

    def set_mode(self, mode, title, sub):
        self.mode, self.mode_col = mode, MODE_COLOR[mode]
        self.h_title.config(text=title)
        self.h_sub.config(text=sub)

    def fade(self, targets, start, delay=0, steps=14):
        """Fade widgets in: targets = [(widget, option, final_colour)]."""
        for w, opt, _ in targets:
            w.config(**{opt: start})

        def step(n):
            e = 1 - (1 - n / steps) ** 3
            try:
                for w, opt, col in targets:
                    w.config(**{opt: lerp(start, col, e)})
            except tk.TclError:
                return
            if n < steps:
                self.after(16, step, n + 1)
        self.after(delay, step, 0)

    # ================= scan =================
    def start(self, keep_log=False):
        if self.running:
            return
        self.running = self.busy = True
        self.fixing = False
        self.score = None
        if not keep_log:
            self.fix_log = []
        self.btn.set_enabled(False)
        self.btn.set_text("Scanning...")
        self.fix_btn.set_enabled(False)
        self.set_mode("busy", "Scanning your network…", "Running tests")
        for i in range(len(TESTS)):
            self.set_test(i, "pending", "Waiting")
        for tile in self.tiles.values():
            tile.set("—", "scanning...", MUTED)
        self.write_output([], placeholder="Running tests...")
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            facts = diagnose(lambda i, s, d: self.q.put(("test", i, s, d)))
        except Exception:
            facts = {}
        self.q.put(("done", facts))

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "test":
                    self.set_test(*msg[1:])
                elif msg[0] == "fixlog":
                    self.fix_log.append(msg[1:])
                    self.h_sub.config(text=("Done: " if msg[1] else "Failed: ") + msg[2])
                    self.write_output(self.results)
                elif msg[0] == "fixdone":
                    self.running = self.fixing = False
                    self.start(keep_log=True)  # re-scan to verify the fix
                else:
                    self.finish(msg[1])
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def set_test(self, i, status, detail):
        self.test_state[i] = (status, detail)
        self.rows[i].set(status, detail)
        if status == "run" and self.busy and not self.fixing:
            self.h_sub.config(text="Testing: " + TESTS[i])

    def finish(self, facts):
        self.running = self.busy = self.fixing = False
        self.btn.set_enabled(True)
        self.btn.set_text("Run Diagnosis")
        allres = infer(facts)
        self.facts, self.fired = facts, {r[0] for r in allres}
        self.facts_all = {**facts, **{r[2]: True for r in allres}}
        self.results = sorted((r for r in allres if r[3] != "fact"), key=lambda r: SEV_ORDER[r[3]])
        self.score = health_score(facts, self.results) if self.results else None
        self.steps = collect_steps(self.results)
        self.fix_btn.set_enabled(bool(self.steps))
        now = datetime.datetime.now()
        self.stamp.config(text="Last scan  " + now.strftime("%H:%M:%S"))
        self.update_tiles(facts)
        sd, su = facts.get("speed_down"), facts.get("speed_up")
        if not self.results:
            self.set_mode("idle", "No conclusion", "Try running the diagnosis again.")
            title = "No conclusion"
        else:
            top = self.results[0]
            if top[3] == "ok":
                speeds = (f"   ↓ {sd:.1f}" + (f"  ↑ {su:.1f}" if su else "") + " Mbps") if sd else ""
                self.set_mode("ok", "Network is healthy", "All tests passed." + speeds)
                title = "Network is healthy"
            else:
                n = sum(1 for r in self.results if r[3] != "ok")
                self.set_mode(top[3], title_of(top[2]), (f"{n} issues found. " if n > 1 else "") + top[4])
                title = title_of(top[2])
        save_history({"time": now.strftime("%Y-%m-%d  %H:%M"), "score": self.score, "title": title,
                      "down": sd, "up": su, "latency": facts.get("latency_ms")})
        self.write_output(self.results, animate=True)
        self.render_expert()
        self.render_history()

    def update_tiles(self, facts):
        sd, su, lat = facts.get("speed_down"), facts.get("speed_up"), facts.get("latency_ms")
        self.tiles["down"].set(f"{sd:.1f} Mbps" if sd else "—", speed_rating(sd) if sd else "not measured",
                               GREEN if sd and sd >= 10 else AMBER if sd and sd >= 5 else RED if sd else MUTED)
        self.tiles["up"].set(f"{su:.1f} Mbps" if su else "—", "upload" if su else "not measured")
        self.tiles["latency"].set(f"{lat:.0f} ms" if lat else "—",
                                  "packet loss detected" if facts.get("packet_loss") else "no packet loss" if lat else "",
                                  (AMBER if facts.get("high_latency") else TEXT) if lat else MUTED)
        name = facts.get("ssid") or ("Wired / other" if facts.get("has_ip") else "Offline")
        self.tiles["network"].set(name, f"IP {facts['local_ip']}" if facts.get("local_ip") else "no IP address",
                                  TEXT if facts.get("has_ip") else RED)

    # ================= diagnosis panel =================
    def rewrap(self, labels, width):
        for lbl in labels:
            if lbl.winfo_exists():
                lbl.config(wraplength=max(220, width - px(100)))

    def write_output(self, results, placeholder_text=None, animate=False, **kw):
        placeholder_text = placeholder_text or kw.get("placeholder")
        self.diag.clear()
        self.wrap_labels = []
        if not results and not self.fix_log:
            placeholder(self.diag, placeholder_text or "Results will appear here.")
            return
        delay = 0
        for name, cond, conclusion, sev, advice in results:
            self.add_item(sev, title_of(conclusion), ("" if sev == "ok" else "Fix: ") + advice,
                          f"Why ({name}): " + why_text(cond), conclusion in FIXES, animate, delay)
            delay += 150
        if self.fix_log:
            self.add_log(animate, delay)

    def add_item(self, sev, title, fix, why, fixable, animate, delay):
        color = SEV_COLOR[sev]
        box = tk.Frame(self.diag.inner, bg=CARD2)
        box.pack(fill="x", pady=(0, px(10)), padx=(0, px(8)))
        bar = tk.Frame(box, bg=color, width=px(4))
        bar.pack(side="left", fill="y")
        body = tk.Frame(box, bg=CARD2)
        body.pack(side="left", fill="x", expand=True, padx=px(14), pady=px(10))
        top = tk.Frame(body, bg=CARD2)
        top.pack(fill="x")
        t = tk.Label(top, text=title, bg=CARD2, fg=color, font=(FONT, 12, "bold"), anchor="w")
        t.pack(side="left")
        targets = [(bar, "bg", color), (t, "fg", color)]
        if fixable and sev != "ok":
            chip = tk.Label(top, text=" AUTO-FIXABLE ", bg=lerp(CARD2, GREEN, .18), fg=GREEN, font=(FONT, 8, "bold"))
            chip.pack(side="right")
            targets += [(chip, "bg", lerp(CARD2, GREEN, .18)), (chip, "fg", GREEN)]
        f = tk.Label(body, text=fix, bg=CARD2, fg=TEXT, font=(FONT, 10), anchor="w", justify="left",
                     wraplength=self.diag.width())
        f.pack(fill="x", pady=(px(6), px(2)))
        w = tk.Label(body, text=why, bg=CARD2, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=self.diag.width())
        w.pack(fill="x")
        self.wrap_labels += [f, w]
        targets += [(f, "fg", TEXT), (w, "fg", MUTED)]
        if animate:
            self.fade(targets, CARD2, delay)

    def add_log(self, animate, delay):
        box = tk.Frame(self.diag.inner, bg=CARD2)
        box.pack(fill="x", pady=(0, px(10)), padx=(0, px(8)))
        body = tk.Frame(box, bg=CARD2)
        body.pack(fill="x", padx=px(14), pady=px(10))
        head = tk.Label(body, text="Auto Fix log", bg=CARD2, fg=ACCENT, font=(FONT, 12, "bold"), anchor="w")
        head.pack(fill="x")
        targets = [(head, "fg", ACCENT)]
        for ok, label, detail in self.fix_log:
            col = GREEN if ok else RED
            line = tk.Label(body, text=("✔  " if ok else "✖  ") + label + (f"  -  {detail}" if detail else ""),
                            bg=CARD2, fg=col, font=(FONT, 9), anchor="w", justify="left",
                            wraplength=self.diag.width())
            line.pack(fill="x", pady=(px(3), 0))
            self.wrap_labels.append(line)
            targets.append((line, "fg", col))
        if animate:
            self.fade(targets, CARD2, delay)

    # ================= auto fix =================
    def auto_fix(self):
        if self.running or not self.steps:
            return
        if not IS_WIN:
            messagebox.showinfo("Auto Fix", "Auto Fix is available on Windows only.\n"
                                "Please follow the 'Fix' instructions shown.")
            return
        if not is_admin():
            if messagebox.askyesno("Administrator needed", "Auto Fix needs administrator rights.\n"
                                   "Restart the app as administrator?") and relaunch_admin():
                self.destroy()
            return
        listing = "\n".join("• " + label for label, _ in self.steps)
        if not messagebox.askyesno("Apply fixes?", "These steps will be applied:\n\n" + listing +
                                   "\n\nContinue?"):
            return
        self.running = self.busy = self.fixing = True
        self.fix_total, self.fix_log = len(self.steps), []
        self.btn.set_enabled(False)
        self.fix_btn.set_enabled(False)
        self.set_mode("busy", "Applying fixes…", "Running repair steps")
        self.write_output(self.results)
        threading.Thread(target=self.fix_worker, args=(list(self.steps),), daemon=True).start()

    def fix_worker(self, steps):
        for label, args in steps:
            ok, detail = run_step(args)
            self.q.put(("fixlog", ok, label, detail))
            if label.startswith("Turn on"):
                time.sleep(6)  # give Wi-Fi time to reconnect to the saved network
        time.sleep(4)  # let the network settle before re-scanning
        self.q.put(("fixdone",))

    # ================= export =================
    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="network_report.txt",
                                            filetypes=[("Text file", "*.txt")])
        if not path:
            return
        f = self.facts
        lines = ["NETWORK DIAGNOSIS REPORT", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 f"Health score: {self.score if self.score is not None else 'n/a'} / 100",
                 f"Network: {f.get('ssid') or 'n/a'}   IP: {f.get('local_ip') or 'n/a'}   Gateway: {f.get('gateway') or 'n/a'}",
                 "", "TESTS"]
        for name, (status, detail) in zip(TESTS, self.test_state):
            lines.append(f"  [{status.upper():4}] {name}: {detail}")
        lines += ["", "DIAGNOSIS"]
        for name, cond, conclusion, severity, advice in self.results:
            lines += [f"  {title_of(conclusion)} ({severity})", f"    Fix: {advice}",
                      f"    Why ({name}): {why_text(cond)}"]
        if self.fix_log:
            lines += ["", "AUTO FIX LOG"] + [f"  [{'OK' if ok else 'FAIL'}] {label} {detail}"
                                              for ok, label, detail in self.fix_log]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        messagebox.showinfo("Saved", "Report saved.")


if __name__ == "__main__":
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    App().mainloop()
