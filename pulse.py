#!/usr/bin/env python3
"""
pulse — ncurses window-first TUI (Termux / Linux).
Адаптивная сетка: альбомная 2×2 + низ, портретная — полосы.
"""

from __future__ import annotations

import curses
import json
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

REFRESH_SEC = 5
RESIZE_DEBOUNCE = 0.45
# Любой размер Termux — без требования «свернуть клавиатуру»
MIN_COLS, MIN_LINES = 8, 2
PANEL_MIN_H = 2
# Меньше — одна панель PULSE, иначе в каждой полосе видна только 1 строка
MIN_BODY_FOR_GRID = 13
MIN_PANEL_BODY = 5

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"
CRYPTO_PAIRS = {"btc": "BTC-USDT", "eth": "ETH-USDT", "rub": "USDT-RUB"}
USER_AGENT = "pulse-dashboard/3.3"
SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: float = 10.0, extra_headers: Optional[dict] = None) -> str:
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""


# ── State & snapshot ────────────────────────────────────────────────────────────

@dataclass
class AppState:
    prev_btc: Optional[str] = None
    prev_eth: Optional[str] = None
    session_btc: Optional[str] = None
    session_eth: Optional[str] = None
    ping_cf_ms: List[int] = field(default_factory=list)
    ping_gg_ms: List[int] = field(default_factory=list)
    pct_hist: List[float] = field(default_factory=list)
    last_weather: str = ""
    last_weather_ts: float = 0.0
    last_ip: str = ""
    last_ip_ts: float = 0.0
    api_ok: bool = True
    # Сеть: обновление не чаще REFRESH_SEC (пинг тяжёлый)
    net_ts: float = 0.0
    snap_ip: str = "n/a"
    snap_ping_cf: str = "n/a"
    snap_ping_gg: str = "n/a"
    snap_net_cf_line: str = ""
    snap_net_gg_line: str = ""
    snap_net_quality: str = "n/a"


@dataclass
class Snapshot:
    msk: str = "n/a"
    utc: str = "n/a"
    weather: str = "n/a"
    uptime: str = "n/a"
    btc: str = ""
    eth: str = ""
    rub: str = ""
    ip: str = "n/a"
    ping_cf: str = "n/a"
    ping_gg: str = "n/a"
    net_cf_line: str = ""
    net_gg_line: str = ""
    net_quality: str = ""
    feed_lines: List[str] = field(default_factory=list)
    btc_mom: str = "n/a"
    eth_mom: str = "n/a"
    vol_line: str = "n/a"
    spark_line: str = "n/a"
    market_extra: List[str] = field(default_factory=list)


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_time() -> Tuple[str, str]:
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        msk = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M  %d.%m")
        utc = datetime.now(ZoneInfo("UTC")).strftime("%H:%M  %d.%m")
    except Exception:
        msk = time.strftime("%H:%M  %d.%m")
        utc = msk
    return msk, utc


def fetch_uptime() -> str:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            sec = float(f.read().split()[0])
        h, rem = divmod(int(sec), 3600)
        m, _ = divmod(rem, 60)
        return f"up {h}h {m}m"
    except OSError:
        return "up n/a"


def fetch_weather(state: AppState) -> str:
    now = time.time()
    if state.last_weather and (now - state.last_weather_ts) < 90:
        return state.last_weather
    raw = _http_get(
        "https://wttr.in/Saint_Petersburg?format=3",
        extra_headers={"User-Agent": "curl"},
    )
    raw = raw.replace("\n", " ").strip()
    if not raw:
        return "SPB: n/a"
    state.last_weather = raw
    state.last_weather_ts = now
    return raw


def fetch_coinbase_spot(pair: str) -> str:
    url = COINBASE_SPOT_URL.format(pair=pair)
    body = _http_get(url, timeout=10.0)
    if not body:
        return ""
    try:
        amount = json.loads(body).get("data", {}).get("amount")
        return str(amount) if amount is not None else ""
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


def fetch_crypto(state: AppState) -> Tuple[str, str, str]:
    out: Dict[str, str] = {k: "" for k in CRYPTO_PAIRS}
    ok = False
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(fetch_coinbase_spot, pair): key
            for key, pair in CRYPTO_PAIRS.items()
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
                if out[key]:
                    ok = True
            except Exception:
                out[key] = ""
    state.api_ok = ok
    return out["btc"], out["eth"], out["rub"]


def fmt_price(raw: str) -> str:
    if not raw:
        return "n/a"
    try:
        n = float(raw)
    except ValueError:
        return "n/a"
    if n >= 1000:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:.4f}"
    return f"{n:.6f}"


def fetch_ip(state: AppState) -> str:
    now = time.time()
    if state.last_ip and (now - state.last_ip_ts) < 45:
        return state.last_ip
    ip = _http_get("https://api.ipify.org").strip() or "n/a"
    state.last_ip = ip
    state.last_ip_ts = now
    return ip


def ping_ms(host: str) -> Tuple[str, Optional[int]]:
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=4,
        )
        out = proc.stdout + proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "n/a", None
    for token in out.split():
        if token.startswith("time="):
            val = token.split("=", 1)[1].replace("ms", "")
            try:
                n = int(float(val))
                return f"{n}ms", n
            except ValueError:
                pass
    return "n/a", None


def push_ping(state: AppState, hist: List[int], ms: Optional[int], limit: int = 16) -> None:
    if ms is not None:
        hist.append(ms)
    while len(hist) > limit:
        hist.pop(0)


def numeric_sparkline(values: List[int], width: int = 12) -> str:
    """Высота баров = относительно min..max в истории (виден рост/падение)."""
    if not values:
        return "·" * width
    sample = values[-width:]
    lo, hi = min(sample), max(sample)
    if lo == hi:
        mid = len(SPARK_CHARS) // 2
        return SPARK_CHARS[mid] * len(sample)
    out = []
    for v in sample:
        idx = int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def trend_arrow(values: List[int]) -> str:
    if len(values) < 2:
        return "→"
    a, b = values[-2], values[-1]
    if b < a - 2:
        return "↓"
    if b > a + 2:
        return "↑"
    return "→"


def refresh_network(state: AppState, interval: int) -> None:
    """Пинг и IP — только раз в interval секунд."""
    now = time.time()
    if state.net_ts and (now - state.net_ts) < interval:
        return

    state.net_ts = now
    state.snap_ip = fetch_ip(state)
    cf, cf_ms = ping_ms("1.1.1.1")
    gg, gg_ms = ping_ms("google.com")
    push_ping(state, state.ping_cf_ms, cf_ms)
    push_ping(state, state.ping_gg_ms, gg_ms)

    state.snap_ping_cf = cf
    state.snap_ping_gg = gg
    state.snap_net_cf_line = ping_panel_line("CF", state.ping_cf_ms, cf_ms)
    state.snap_net_gg_line = ping_panel_line("GG", state.ping_gg_ms, gg_ms)
    if state.ping_cf_ms or state.ping_gg_ms:
        all_p = state.ping_cf_ms + state.ping_gg_ms
        state.snap_net_quality = f"Q {sum(all_p) // len(all_p)}ms {trend_arrow(all_p)}"
    else:
        state.snap_net_quality = "Q n/a"


def ping_panel_line(label: str, hist: List[int], now_ms: Optional[int]) -> str:
    if not hist:
        return f"{label}  n/a"
    now = now_ms if now_ms is not None else hist[-1]
    lo, hi = min(hist), max(hist)
    avg = sum(hist) // len(hist)
    spark = numeric_sparkline(hist, 10)
    arr = trend_arrow(hist)
    return f"{label} {now:>4}ms {arr} [{lo}-{hi}] avg{avg} {spark}"


def pct_change(cur: str, prev: Optional[str]) -> float:
    if not cur or not prev:
        return 0.0
    try:
        c, p = float(cur), float(prev)
    except ValueError:
        return 0.0
    return ((c - p) / p * 100.0) if p else 0.0


def pct_sparkline(pcts: List[float], width: int = 10) -> str:
    if not pcts:
        return "·" * width
    sample = pcts[-width:]
    lo, hi = min(sample), max(sample)
    if abs(hi - lo) < 1e-9:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(sample)
    out = []
    for p in sample:
        idx = int((p - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def momentum_line(sym: str, cur: str, prev: Optional[str], interval: int) -> str:
    if not cur:
        return f"{sym}: n/a"
    pct = pct_change(cur, prev)
    if prev is None:
        arrow, pct = "→", 0.0
    elif pct > 0.02:
        arrow = "↑"
    elif pct < -0.02:
        arrow = "↓"
    else:
        arrow = "→"
    sign = "+" if pct >= 0 else ""
    return f"{sym}: {arrow} {sign}{pct:.2f}% /{interval}s"


def build_feed_lines(state: AppState, btc: str, eth: str) -> List[str]:
    lines: List[str] = []
    api = "Coinbase OK" if state.api_ok else "Coinbase FAIL"
    lines.append(f"API  {api}")

    if state.session_btc and btc:
        d = pct_change(btc, state.session_btc)
        sign = "+" if d >= 0 else ""
        lines.append(f"BTC sess {sign}{d:.2f}%")
    if state.session_eth and eth:
        d = pct_change(eth, state.session_eth)
        sign = "+" if d >= 0 else ""
        lines.append(f"ETH sess {sign}{d:.2f}%")

    if state.pct_hist:
        last = state.pct_hist[-6:]
        nums = " ".join(f"{p:+.2f}" for p in last)
        lines.append(f"tick {nums}")
        lines.append(f"     {pct_sparkline(state.pct_hist, 14)}")

    return lines[:5]


def build_snapshot(state: AppState, interval: int) -> Snapshot:
    snap = Snapshot()
    snap.msk, snap.utc = fetch_time()
    snap.uptime = fetch_uptime()
    snap.weather = fetch_weather(state)

    btc, eth, rub = fetch_crypto(state)
    if state.session_btc is None and btc:
        state.session_btc = btc
    if state.session_eth is None and eth:
        state.session_eth = eth
    snap.btc, snap.eth, snap.rub = btc, eth, rub

    refresh_network(state, interval)
    snap.ip = state.snap_ip
    snap.ping_cf = state.snap_ping_cf
    snap.ping_gg = state.snap_ping_gg
    snap.net_cf_line = state.snap_net_cf_line
    snap.net_gg_line = state.snap_net_gg_line
    snap.net_quality = state.snap_net_quality

    pct = pct_change(btc, state.prev_btc) if state.prev_btc and btc else 0.0
    state.pct_hist.append(pct)
    state.pct_hist = state.pct_hist[-24:]

    snap.btc_mom = momentum_line("BTC", btc, state.prev_btc, interval)
    snap.eth_mom = momentum_line("ETH", eth, state.prev_eth, interval)

    if state.pct_hist:
        last = state.pct_hist[-5:]
        vol_nums = " ".join(f"{p:+.2f}" for p in last)
        snap.vol_line = f"VOL% {vol_nums}"
        snap.spark_line = (
            f"BTC {pct_sparkline(state.pct_hist, 16)} "
            f"{trend_arrow([int(x * 100) for x in last])}"
        )
    else:
        snap.vol_line = "VOL% n/a"
        snap.spark_line = "BTC ················"

    snap.market_extra = []
    if rub:
        snap.market_extra.append(f"USDT/RUB {fmt_price(rub)}")
    if state.pct_hist:
        snap.market_extra.append(
            f"σ {max(abs(x) for x in state.pct_hist[-8:]):.3f}%"
        )

    snap.feed_lines = build_feed_lines(state, btc, eth)

    if btc:
        state.prev_btc = btc
    if eth:
        state.prev_eth = eth
    return snap


# ── Grid (landscape / portrait) ───────────────────────────────────────────────

@dataclass(frozen=True)
class GridCell:
    y: int
    x: int
    h: int
    w: int


@dataclass
class LayoutPlan:
    mode: str  # full | compact | minimal | micro
    cells: Dict[str, GridCell]
    portrait: bool
    show_feed: bool = True
    show_network: bool = True
    show_market: bool = True


def _split_height(total: int, n: int, min_h: int = PANEL_MIN_H) -> Tuple[List[int], int]:
    """Делит body на n полос; возвращает (высоты, фактическое число полос)."""
    if n <= 0 or total <= 0:
        return [], 0
    if total < n * min_h:
        n = max(1, total // min_h)
    base, rem = divmod(total, n)
    heights = [base + (1 if i < rem else 0) for i in range(n)]
    return heights, n


class GridLayout:
    @staticmethod
    def compute(screen_h: int, screen_w: int) -> LayoutPlan:
        screen_w = max(MIN_COLS, screen_w)
        screen_h = max(MIN_LINES, screen_h)
        portrait = screen_h > screen_w

        body = max(1, screen_h)

        def _micro_plan() -> LayoutPlan:
            return LayoutPlan(
                mode="micro",
                cells={"micro": GridCell(0, 0, body, screen_w)},
                portrait=portrait,
                show_feed=False,
                show_network=False,
                show_market=False,
            )

        # Одна панель: на крошечном экране или когда сетка = по 1 строке в блоке
        if body < MIN_BODY_FOR_GRID or screen_w < 22:
            return _micro_plan()
        if portrait and screen_w < 56 and body < 16:
            return _micro_plan()
        if body // MIN_PANEL_BODY < 2:
            return _micro_plan()

        # minimal: 2–3 полосы (только если каждая полоса ≥5 строк)
        if body < 9 or screen_w < 28:
            keys = ["info", "crypto", "market"]
            hs, n = _split_height(body, len(keys))
            keys = keys[:n]
            y, cells = 0, {}
            for key, h in zip(keys, hs):
                cells[key] = GridCell(y, 0, h, screen_w)
                y += h
            return LayoutPlan(
                mode="minimal",
                cells=cells,
                portrait=True,
                show_feed=False,
                show_network=False,
                show_market=True,
            )

        # compact: узкий терминал (95×17 — full с рамками)
        if body < 11 or screen_w < 40:
            if portrait or screen_w < 40:
                if body < 16:
                    return _micro_plan()
                keys = ["info", "feed", "crypto", "network", "market"]
                hs, n = _split_height(body, len(keys))
                keys = keys[:n]
                y, cells = 0, {}
                for key, h in zip(keys, hs):
                    cells[key] = GridCell(y, 0, h, screen_w)
                    y += h
                return LayoutPlan(
                    mode="compact",
                    cells=cells,
                    portrait=True,
                    show_feed="feed" in keys,
                    show_network="network" in keys,
                    show_market=True,
                )

            # альбом: 3 ряда × 2 колонки
            rows, _ = _split_height(body, 3)
            if len(rows) < 3:
                rows = (rows + [max(PANEL_MIN_H, body - sum(rows))])[:3]
            half = screen_w // 2
            rw = screen_w - half
            y = 0
            cells = {
                "info": GridCell(y, 0, rows[0], half),
                "feed": GridCell(y, half, rows[0], rw),
                "crypto": GridCell(y := y + rows[0], 0, rows[1], half),
                "network": GridCell(y, half, rows[1], rw),
                "market": GridCell(y + rows[1], 0, rows[2], screen_w),
            }
            return LayoutPlan(
                mode="compact",
                cells=cells,
                portrait=False,
                show_feed=True,
                show_network=True,
                show_market=True,
            )

        # full portrait: info, feed, crypto|net, market
        if portrait:
            hs, _ = _split_height(body, 4)
            while len(hs) < 4:
                hs.append(PANEL_MIN_H)
            y = 0
            cells: Dict[str, GridCell] = {
                "info": GridCell(y, 0, hs[0], screen_w),
            }
            y += hs[0]
            cells["feed"] = GridCell(y, 0, hs[1], screen_w)
            y += hs[1]
            half = screen_w // 2
            cells["crypto"] = GridCell(y, 0, hs[2], half)
            cells["network"] = GridCell(y, half, hs[2], screen_w - half)
            y += hs[2]
            cells["market"] = GridCell(y, 0, hs[3], screen_w)
            return LayoutPlan(mode="full", cells=cells, portrait=True)

        rows, _ = _split_height(body, 3)
        while len(rows) < 3:
            rows.append(max(PANEL_MIN_H, body - sum(rows)))
        half = screen_w // 2
        rw = screen_w - half
        return LayoutPlan(
            mode="full",
            cells={
                "info": GridCell(0, 0, rows[0], half),
                "feed": GridCell(0, half, rows[0], rw),
                "crypto": GridCell(rows[0], 0, rows[1], half),
                "network": GridCell(rows[0], half, rows[1], rw),
                "market": GridCell(rows[0] + rows[1], 0, rows[2], screen_w),
            },
            portrait=False,
        )


def _clip(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…" if max_len > 1 else text[:1]


class WindowComponent:
    title: str = ""
    color_pair: int = 0

    def __init__(self, cell: GridCell) -> None:
        self.cell = cell
        h = max(1, cell.h)
        w = max(1, cell.w)
        self.win = curses.newwin(h, w, cell.y, cell.x)

    def _draw_chrome(self) -> None:
        self.win.border()
        label = f" {self.title} "
        try:
            self.win.addstr(
                0, 2, _clip(label, self.cell.w - 4),
                curses.color_pair(self.color_pair) | curses.A_BOLD,
            )
        except curses.error:
            pass

    def _text(self, row: int, col: int, value: str) -> None:
        if row <= 0 or row >= self.cell.h - 1:
            return
        max_len = self.cell.w - col - 2
        if max_len <= 0:
            return
        try:
            self.win.addstr(
                row, col, _clip(value, max_len), curses.color_pair(self.color_pair)
            )
        except curses.error:
            pass

    def _begin_draw(self) -> None:
        self.win.clear()
        self._draw_chrome()

    def _end_draw(self) -> None:
        self.win.refresh()

    def _max_lines(self) -> int:
        # рамка: верх (заголовок) + низ
        return max(0, self.cell.h - 2)

    def _draw_lines(self, start_row: int, lines: List[str]) -> None:
        last_row = self.cell.h - 2
        for i, line in enumerate(lines):
            row = start_row + i
            if row > last_row:
                break
            self._text(row, 2, line)


class MicroWindow(WindowComponent):
    title = "PULSE"
    color_pair = 1

    def draw(self, snap: Snapshot) -> None:
        self._begin_draw()
        lines = [
            f"MSK {snap.msk}   UTC {snap.utc}",
            f"WX  {snap.weather}",
            f"BTC {fmt_price(snap.btc)}   ETH {fmt_price(snap.eth)}",
            f"RUB {fmt_price(snap.rub)}   {snap.btc_mom}",
            f"IP  {snap.ip}",
            f"CF {snap.ping_cf}   GG {snap.ping_gg}",
            snap.net_cf_line,
            snap.vol_line,
            f"{snap.net_quality}",
        ]
        self._draw_lines(1, lines)
        self._end_draw()


class InfoWindow(WindowComponent):
    title = "INFO"
    color_pair = 1

    def draw(self, snap: Snapshot, extra: Optional[List[str]] = None) -> None:
        self._begin_draw()
        lines = [
            f"MSK {snap.msk}",
            f"UTC {snap.utc}",
            f"WX  {snap.weather}",
            f"{snap.uptime}",
        ]
        if extra:
            lines.extend(extra)
        if self._max_lines() <= 2:
            lines = [
                f"MSK {snap.msk}  {snap.weather}",
                f"UTC {snap.utc}  {snap.uptime}",
            ]
        self._draw_lines(2, lines)
        self._end_draw()


class FeedWindow(WindowComponent):
    title = "FEED"
    color_pair = 2

    def draw(self, snap: Snapshot) -> None:
        self._begin_draw()
        if snap.feed_lines:
            self._draw_lines(2, snap.feed_lines)
        else:
            self._text(2, 2, "waiting data…")
        self._end_draw()


class CryptoWindow(WindowComponent):
    title = "CRYPTO"
    color_pair = 3

    def draw(self, snap: Snapshot) -> None:
        self._begin_draw()
        lines = [
            f"BTC/USDT  {fmt_price(snap.btc)}",
            f"ETH/USDT  {fmt_price(snap.eth)}",
            f"USDT/RUB  {fmt_price(snap.rub)}",
        ]
        if snap.btc and snap.eth and snap.rub:
            try:
                b, e, r = float(snap.btc), float(snap.eth), float(snap.rub)
                lines.append(f"1 BTC = {b*r/e:,.0f} RUB")
            except ValueError:
                pass
        self._draw_lines(2, lines)
        self._end_draw()


class NetworkWindow(WindowComponent):
    title = "NETWORK"
    color_pair = 4

    def draw(self, snap: Snapshot) -> None:
        self._begin_draw()
        lines = [
            f"IP  {snap.ip}",
            snap.net_cf_line,
            snap.net_gg_line,
            snap.net_quality,
        ]
        self._draw_lines(2, lines)
        self._end_draw()


class MarketWindow(WindowComponent):
    title = "MARKET"
    color_pair = 5

    def draw(self, snap: Snapshot, include_net: bool = False) -> None:
        self._begin_draw()
        lines = [
            snap.btc_mom,
            snap.eth_mom,
            snap.vol_line,
            snap.spark_line,
            *snap.market_extra,
        ]
        if include_net:
            lines.insert(0, f"CF {snap.ping_cf} GG {snap.ping_gg}")
        if self._max_lines() <= 2:
            lines = [snap.btc_mom, f"CF {snap.ping_cf}  {snap.vol_line}"]
        elif self._max_lines() <= 3:
            lines = [snap.btc_mom, snap.eth_mom, f"CF {snap.ping_cf}"]
        self._draw_lines(2, lines)
        self._end_draw()


class Dashboard:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.stdscr = stdscr
        self._init_colors()
        h, w = stdscr.getmaxyx()
        self.size = (h, w)
        self.plan = GridLayout.compute(h, w)
        self.portrait = self.plan.portrait
        c = self.plan.cells

        self.MICRO_WINDOW: Optional[MicroWindow] = None
        self.INFO_WINDOW: Optional[InfoWindow] = None
        self.FEED_WINDOW: Optional[FeedWindow] = None
        self.CRYPTO_WINDOW: Optional[CryptoWindow] = None
        self.NETWORK_WINDOW: Optional[NetworkWindow] = None
        self.MARKET_WINDOW: Optional[MarketWindow] = None

        self._mount_windows(c)

    def _mount_windows(self, cells: Dict[str, GridCell]) -> None:
        """Создаём subwin с рамками; при ошибке одной панели — остальные остаются."""
        specs = [
            ("micro", MicroWindow, "MICRO_WINDOW"),
            ("info", InfoWindow, "INFO_WINDOW"),
            ("feed", FeedWindow, "FEED_WINDOW"),
            ("crypto", CryptoWindow, "CRYPTO_WINDOW"),
            ("network", NetworkWindow, "NETWORK_WINDOW"),
            ("market", MarketWindow, "MARKET_WINDOW"),
        ]
        for key, cls, attr in specs:
            if key not in cells:
                continue
            if key == "feed" and not self.plan.show_feed:
                continue
            if key == "network" and not self.plan.show_network:
                continue
            if key == "market" and not self.plan.show_market:
                continue
            try:
                setattr(self, attr, cls(cells[key]))
            except curses.error:
                pass

    @staticmethod
    def _init_colors() -> None:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, fg in enumerate(
                (
                    curses.COLOR_GREEN,
                    curses.COLOR_BLUE,
                    curses.COLOR_YELLOW,
                    curses.COLOR_CYAN,
                    curses.COLOR_MAGENTA,
                    curses.COLOR_WHITE,
                ),
                start=1,
            ):
                curses.init_pair(i, fg, -1)

    def configure_input(self) -> None:
        self.stdscr.nodelay(True)
        self.stdscr.timeout(100)

    def render_all(self, snap: Snapshot) -> None:
        if not any(
            (
                self.MICRO_WINDOW,
                self.INFO_WINDOW,
                self.FEED_WINDOW,
                self.CRYPTO_WINDOW,
                self.NETWORK_WINDOW,
                self.MARKET_WINDOW,
            )
        ):
            self._mount_windows(self.plan.cells)
        feed_extra = snap.feed_lines[:2] if not self.FEED_WINDOW else None
        if self.MICRO_WINDOW:
            self.MICRO_WINDOW.draw(snap)
        if self.INFO_WINDOW:
            self.INFO_WINDOW.draw(snap, extra=feed_extra)
        if self.FEED_WINDOW:
            self.FEED_WINDOW.draw(snap)
        if self.CRYPTO_WINDOW:
            self.CRYPTO_WINDOW.draw(snap)
        if self.NETWORK_WINDOW:
            self.NETWORK_WINDOW.draw(snap)
        elif self.MARKET_WINDOW and not self.plan.show_network:
            pass  # net merged visually in market on minimal — optional
        if self.MARKET_WINDOW:
            self.MARKET_WINDOW.draw(
                snap, include_net=not self.plan.show_network
            )

    def poll_input(self) -> Optional[str]:
        """None или 'resize'."""
        try:
            ch = self.stdscr.getch()
        except curses.error:
            return None
        if ch == curses.KEY_RESIZE:
            return "resize"
        return None

    def _destroy_windows(self) -> None:
        for comp in (
            self.MICRO_WINDOW,
            self.INFO_WINDOW,
            self.FEED_WINDOW,
            self.CRYPTO_WINDOW,
            self.NETWORK_WINDOW,
            self.MARKET_WINDOW,
        ):
            if comp is None or not hasattr(comp, "win"):
                continue
            try:
                comp.win.clear()
            except curses.error:
                pass
            try:
                del comp.win
            except (curses.error, AttributeError):
                pass
        self.MICRO_WINDOW = None
        self.INFO_WINDOW = None
        self.FEED_WINDOW = None
        self.CRYPTO_WINDOW = None
        self.NETWORK_WINDOW = None
        self.MARKET_WINDOW = None

    def destroy(self) -> None:
        self._destroy_windows()


def _apply_resize(stdscr: "curses._CursesWindow") -> Tuple[int, int]:
    curses.update_lines_cols()
    h, w = stdscr.getmaxyx()
    try:
        curses.resizeterm(h, w)
    except (curses.error, ValueError, TypeError):
        pass
    return h, w


class ResizeDebouncer:
    """Ждём стабильный размер перед rebuild (клавиатура Termux)."""

    def __init__(self) -> None:
        self.applied: Tuple[int, int] = (0, 0)
        self.pending: Optional[Tuple[int, int]] = None
        self.since: float = 0.0

    def check(self, h: int, w: int) -> bool:
        if (h, w) == self.applied:
            self.pending = None
            return False
        now = time.time()
        if self.pending != (h, w):
            self.pending = (h, w)
            self.since = now
            return False
        if now - self.since >= RESIZE_DEBOUNCE:
            self.applied = (h, w)
            self.pending = None
            return True
        return False


def run_loop(stdscr: "curses._CursesWindow", interval: int) -> None:
    state = AppState()
    ui: Optional[Dashboard] = None
    resize_db = ResizeDebouncer()
    last_snap = Snapshot()
    last_data_at = 0.0

    def rebuild(display: Snapshot) -> Dashboard:
        nonlocal ui
        if ui is not None:
            ui.destroy()
        h, w = _apply_resize(stdscr)
        resize_db.applied = (h, w)
        new_ui = Dashboard(stdscr)
        new_ui.configure_input()
        new_ui.render_all(display)
        return new_ui

    refresh_network(state, interval)
    last_snap = build_snapshot(state, interval)
    last_data_at = time.time()
    ui = rebuild(last_snap)

    try:
        while True:
            h, w = _apply_resize(stdscr)

            if resize_db.check(h, w):
                ui = rebuild(last_snap)
                continue

            now = time.time()
            if now - last_data_at >= interval:
                last_snap = build_snapshot(state, interval)
                last_data_at = now
                try:
                    ui.render_all(last_snap)
                except curses.error:
                    ui.destroy()
                    ui = rebuild(last_snap)

            deadline = last_data_at + interval
            while time.time() < deadline:
                action = ui.poll_input() if ui else None
                if action == "resize":
                    nh, nw = _apply_resize(stdscr)
                    resize_db.check(nh, nw)
                    break
                nh, nw = _apply_resize(stdscr)
                if resize_db.pending is not None:
                    break
                time.sleep(0.2)
    finally:
        ui.destroy()


def main(stdscr: "curses._CursesWindow") -> None:
    interval = max(2, min(5, REFRESH_SEC))
    stdscr.nodelay(True)
    _apply_resize(stdscr)

    while True:
        try:
            run_loop(stdscr, interval)
            return
        except curses.error:
            time.sleep(0.2)
            continue
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    curses.wrapper(main)
