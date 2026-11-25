# code.py - Pico 2 controller screen (CircuitPython 10, 2.8" ILI9341 320x240)
#
# Shows: now playing (album art, title, artist, album, progress), a real-life
# clock, and - while Euro Truck Simulator 2 runs - a truck dashboard (speed,
# speed limit, cruise control, pedals, gear, RPM, blinkers, fuel, lights,
# wipers, park brake, trip info). Data comes from pc/media_bridge.py over USB.
#
# Protocol on the USB data port (one message per line):
#   Media (1/s):  {"t":"Title","a":"Artist","al":"Album","pos":42,"dur":215,
#                  "play":true,"lt":[Y,M,D,h,m,s,weekday]}
#                 t/a/al are ASCII-only fallbacks, shown until images arrive;
#                 lt is the PC's local time, used to set the Pico's clock.
#   ETS2 (10/s):  {"ets":{"spd":87,"lim":80,"cc":true,"ccs":80,"gs":"A9",...}}
#   Image:        "IMG <slot> <w> <h> <bpp> <nbytes>\n" followed by <nbytes> raw bytes
#                 slot "art": 16 bpp RGB565 little-endian, 120x120
#                 slot "title"/"artist"/"album": 4 bpp grey (anti-aliased text drawn
#                 by the PC, so any language works), first pixel in low nibble
#   "HELLO" -> we answer "PICO-MEDIA" (port detection)
#   On start-up we send "READY" so the PC re-sends images after a reload.
#
# Needs on CIRCUITPY:
#   boot.py   (enables the second USB serial port)
#   lib/      adafruit_ili9341.mpy, adafruit_display_text/, adafruit_hid/
#   fonts/    *.bin font sheets for the smooth dashboard digits (tools/make_fonts.py)

import time
import json
import io
import gc
import board
import busio
import displayio
import terminalio
import fourwire
import vectorio
import bitmaptools
import keypad
import usb_cdc
import usb_hid
from adafruit_display_text import label
from adafruit_ili9341 import ILI9341
from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.consumer_control_code import ConsumerControlCode

# ---------------------------------------------------------------- display ---
# If the picture is upside down/mirrored, add rotation=180 to ILI9341(...).
# If album-art colours look wrong (red and blue swapped), use Colorspace.BGR565.
WIDTH, HEIGHT = 320, 240
ART_COLORSPACE = displayio.Colorspace.RGB565

displayio.release_displays()
spi = busio.SPI(clock=board.GP18, MOSI=board.GP19)
bus = fourwire.FourWire(spi, command=board.GP16, chip_select=board.GP17,
                        reset=board.GP20, baudrate=24_000_000)
display = ILI9341(bus, width=WIDTH, height=HEIGHT, backlight_pin=board.GP21)

FONT = terminalio.FONT            # fallback font: 6x12 px, ASCII only

# ----------------------------------------------------------------- layout ---
ART = 120                         # album art size (must match the PC side)
ART_X, ART_Y = 8, 8
TX = ART_X + ART + 12             # text column x
TW = WIDTH - TX - 6               # visible text width


def solid(color):
    p = displayio.Palette(1)
    p[0] = color
    return p


def fade_palette(color, bg=0x000000):
    """16 shades from `bg` to `color` - for 4-bit anti-aliased text."""
    p = displayio.Palette(16)
    fill_fade(p, color, bg)
    return p


def fill_fade(p, color, bg=0x000000):
    r1, g1, b1 = (color >> 16) & 255, (color >> 8) & 255, color & 255
    r0, g0, b0 = (bg >> 16) & 255, (bg >> 8) & 255, bg & 255
    for i in range(16):
        p[i] = (((r0 + (r1 - r0) * i // 15) << 16) | ((g0 + (g1 - g0) * i // 15) << 8)
                | (b0 + (b1 - b0) * i // 15))


def _readline(f):
    out = b""
    while True:
        c = f.read(1)
        if not c or c == b"\n":
            return out
        out += c


class TileText:
    """Smooth (anti-aliased) text from a font sheet made by tools/make_fonts.py.
    Works like a label: set .text and .color. x is the left edge, centre or right
    edge depending on `align`; base_y is the text baseline."""

    def __init__(self, path, max_chars, color, x, base_y, align="left", bg=0x000000):
        with open(path, "rb") as f:
            hdr = _readline(f).split()
            cw, ch, n, self.base, self.pad = (int(v) for v in hdr[1:6])
            chars = _readline(f).decode()
            self.adv = [int(v) for v in _readline(f).split()]
            bmp = displayio.Bitmap(cw * n, ch, 16)
            bitmaptools.readinto(bmp, f, 4)
        self.index = {c: i for i, c in enumerate(chars)}
        self.bg = bg
        self.pal = fade_palette(color, bg)
        self.pal.make_transparent(0)
        self.group = displayio.Group()
        self.tiles = []
        for _ in range(max_chars):
            tg = displayio.TileGrid(bmp, pixel_shader=self.pal, width=1, height=1,
                                    tile_width=cw, tile_height=ch, default_tile=0)
            tg.hidden = True
            self.group.append(tg)
            self.tiles.append(tg)
        self.x, self.base_y, self.align = x, base_y, align
        self._text, self._color, self.width = None, color, 0
        self.text = ""

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, s):
        if s == self._text:
            return
        self._text = s
        s = s[:len(self.tiles)]
        keys = [self.index.get(c, 0) for c in s]
        self.width = sum(self.adv[k] for k in keys)
        if self.align == "centre":
            cur = self.x - self.width // 2
        elif self.align == "right":
            cur = self.x - self.width
        else:
            cur = self.x
        for i, tg in enumerate(self.tiles):
            if i < len(keys):
                tg[0] = keys[i]
                tg.x = cur - self.pad
                tg.y = self.base_y - self.base
                tg.hidden = False
                cur += self.adv[keys[i]]
            else:
                tg.hidden = True

    @property
    def color(self):
        return self._color

    @color.setter
    def color(self, c):
        if c != self._color:
            self._color = c
            fill_fade(self.pal, c, self.bg)
            self.pal.make_transparent(0)

    def move(self, x):
        self.x = x
        t, self._text = self._text, None
        self.text = t


class LabelText:
    """Fallback with the same interface, using the built-in font (if a .bin is missing)."""

    def __init__(self, max_chars, color, x, base_y, align="left", scale=1):
        anchor = {"left": 0.0, "centre": 0.5, "right": 1.0}[align]
        self.lbl = label.Label(FONT, text="", color=color, scale=scale,
                               anchor_point=(anchor, 1.0), anchored_position=(x, base_y))
        self.group, self.scale, self.anchor_x, self.base_y = self.lbl, scale, anchor, base_y
        self.width = 0

    @property
    def text(self):
        return self.lbl.text

    @text.setter
    def text(self, s):
        if s != self.lbl.text:
            self.lbl.text = s
            self.width = len(s) * 6 * self.scale

    @property
    def color(self):
        return self.lbl.color

    @color.setter
    def color(self, c):
        self.lbl.color = c

    def move(self, x):
        self.lbl.anchored_position = (x, self.base_y)


def make_text(sheet, max_chars, color, x, base_y, align="left", bg=0x000000, fallback_scale=1):
    try:
        return TileText("/fonts/%s.bin" % sheet, max_chars, color, x, base_y, align, bg)
    except (OSError, ValueError) as e:
        print("font sheet %s not usable (%s) - using built-in font" % (sheet, e))
        return LabelText(max_chars, color, x, base_y, align, fallback_scale)


class Line:
    """One text line: shows a PC-drawn image if we have one, else an ASCII label.
    Scrolls sideways when it is wider than the text column."""

    def __init__(self, y, height, color, scale):
        self.group = displayio.Group(x=TX, y=y)
        self.palette = fade_palette(color)
        self.palette.make_transparent(0)          # lines can sit close without clipping each other
        self.scale = scale
        self.label = label.Label(FONT, text="", color=color, scale=scale,
                                 anchor_point=(0, 0.5),
                                 anchored_position=(0, height // 2))
        self.group.append(self.label)
        self.image = None
        self.width = 0
        self.offset = 0
        self.pause_until = 0.0

    def _drop_image(self):
        if self.image is not None:
            self.group.remove(self.image)
            self.image = None

    def set_text(self, text):
        self._drop_image()
        self.label.text = text
        self.label.hidden = False
        self.width = len(text) * 6 * self.scale
        self.restart()

    def set_image(self, bmp):
        self._drop_image()
        self.image = displayio.TileGrid(bmp, pixel_shader=self.palette)
        self.group.append(self.image)
        self.label.hidden = True
        self.width = bmp.width
        self.restart()

    def restart(self):
        self.offset = 0
        self.pause_until = time.monotonic() + 2
        self.group.x = TX

    def scroll(self, now):
        overflow = self.width - TW
        if overflow <= 0:
            self.group.x = TX
            return
        if now < self.pause_until:
            return
        if self.offset >= overflow:          # was holding at the end -> jump back
            self.offset = 0
            self.pause_until = now + 2
        else:
            self.offset = min(self.offset + 2, overflow)
            if self.offset == overflow:      # reached the end -> hold
                self.pause_until = now + 2
        self.group.x = TX - self.offset


root = displayio.Group()
display.root_group = root
root.append(vectorio.Rectangle(pixel_shader=solid(0x000000), width=WIDTH, height=HEIGHT))

lines = {
    "title": Line(y=4, height=28, color=0xFFFFFF, scale=2),
    "artist": Line(y=36, height=20, color=0xB3B3B3, scale=1),
    "album": Line(y=54, height=20, color=0x808080, scale=1),
}
for ln in lines.values():
    root.append(ln.group)

# Black mask left of the text column: scrolling text slides "under" the art.
root.append(vectorio.Rectangle(pixel_shader=solid(0x000000),
                               width=TX - 2, height=ART_Y + ART + 4))
# Grey placeholder, then the album art on top of it.
root.append(vectorio.Rectangle(pixel_shader=solid(0x282828),
                               width=ART, height=ART, x=ART_X, y=ART_Y))
art_bmp = displayio.Bitmap(ART, ART, 65536)
art_tg = displayio.TileGrid(art_bmp, x=ART_X, y=ART_Y,
                            pixel_shader=displayio.ColorConverter(input_colorspace=ART_COLORSPACE))
art_tg.hidden = True
root.append(art_tg)

# Progress bar + times + state at the bottom.
# (The space between the art and the bar is free for ETS2 info later.)
BAR_X, BAR_Y, BAR_W, BAR_H = 8, HEIGHT - 36, WIDTH - 16, 6
root.append(vectorio.Rectangle(pixel_shader=solid(0x303030), width=BAR_W, height=BAR_H,
                               x=BAR_X, y=BAR_Y))
bar_fg = vectorio.Rectangle(pixel_shader=solid(0x1DB954), width=1, height=BAR_H,
                            x=BAR_X, y=BAR_Y)
root.append(bar_fg)
# Song time "1:11 / 3:20" as three pieces so the slash gets a small gap (SLASH_GAP px)
# on each side - narrower than a full space in the built-in 6 px font.
SLASH_GAP = 2
time_lbl = label.Label(FONT, text="", color=0xB3B3B3,
                       anchor_point=(0.0, 0.5), anchored_position=(BAR_X, HEIGHT - 16))
slash_lbl = label.Label(FONT, text="", color=0xB3B3B3,
                        anchor_point=(0.0, 0.5), anchored_position=(BAR_X, HEIGHT - 16))
total_lbl = label.Label(FONT, text="", color=0xB3B3B3,
                        anchor_point=(0.0, 0.5), anchored_position=(BAR_X, HEIGHT - 16))
for _l in (time_lbl, slash_lbl, total_lbl):
    root.append(_l)
times_end = BAR_X                                  # right edge of the song time text


def set_times(elapsed, total):
    global times_end
    if elapsed == time_lbl.text and total == total_lbl.text:
        return
    time_lbl.text = elapsed
    if not elapsed:
        slash_lbl.text = total_lbl.text = ""
        times_end = BAR_X
        return
    x = BAR_X + len(elapsed) * 6 + SLASH_GAP
    slash_lbl.text = "/"
    slash_lbl.anchored_position = (x, HEIGHT - 16)
    x += 6 + SLASH_GAP
    total_lbl.text = total
    total_lbl.anchored_position = (x, HEIGHT - 16)
    times_end = x + len(total) * 6
state_lbl = label.Label(FONT, text="Waiting for PC...", color=0xB3B3B3,
                        anchor_point=(1.0, 0.5), anchored_position=(WIDTH - 8, HEIGHT - 16))
root.append(state_lbl)

# Real-life clock (bottom centre). The PC sends its local time; the Pico sets its own
# RTC from it and keeps ticking by itself, so it runs even if the PC stops sending.
import rtc
_rtc = rtc.RTC()
clock_lbl = label.Label(FONT, text="", color=0xB3B3B3,
                        anchor_point=(0.5, 0.5), anchored_position=(BAR_X + BAR_W // 2, HEIGHT - 16))
root.append(clock_lbl)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
clock_set = False


def sync_clock(lt):
    """lt = [year, month, day, hour, minute, second, weekday(Mon=0)] from the PC."""
    global clock_set
    try:
        pc_time = time.struct_time((lt[0], lt[1], lt[2], lt[3], lt[4], lt[5], lt[6], -1, -1))
        if not clock_set or abs(time.mktime(time.localtime()) - time.mktime(pc_time)) > 2:
            _rtc.datetime = pc_time                    # only re-sync when it has drifted
            clock_set = True
    except (ValueError, OverflowError, IndexError, TypeError):
        pass


def update_clock():
    if not clock_set:
        return
    t = time.localtime()
    set_text(clock_lbl, "%s %d %s %02d:%02d" % (DAYS[t.tm_wday], t.tm_mday,
                                                MONTHS[t.tm_mon - 1], t.tm_hour, t.tm_min))
    # Centred on the screen. Only if the text beside it is long enough to crowd it
    # (e.g. "Waiting for PC..."), centre it in the free gap instead (6 px per character).
    left_end = times_end
    right_start = (WIDTH - 8) - len(state_lbl.text) * 6
    half = len(clock_lbl.text) * 6 // 2
    x = WIDTH // 2
    if x - half < left_end + 12 or x + half > right_start - 12:
        x = (left_end + right_start) // 2
    if clock_lbl.anchored_position[0] != x:
        clock_lbl.anchored_position = (x, HEIGHT - 16)

# ------------------------------------------------------------ ETS2 dashboard ---
# Lives in the space between the album art and the progress bar.
# Shown only while ETS2 data arrives ({"ets": {...}} lines from the PC).
GREEN, AMBER, RED, BLUE, DIM = 0x1DB954, 0xFFB000, 0xE02020, 0x3B82F6, 0x303030
YELLOW, WIPER_ON = 0xFFC800, 0x4FC3F7
ROW1 = 152                            # vertical centre of the speed row
# Row 2 sits halfway between the bottom of the RPM bar (ROW1 + 24) and the
# top of the music progress bar (HEIGHT - 36), so the gaps above and below match.
ROW2 = ((ROW1 + 24) + (HEIGHT - 36)) // 2

ets_group = displayio.Group()
ets_group.hidden = True
root.append(ets_group)

def _lbl(x, y, scale=1, color=0xFFFFFF, anchor=(0, 0.5)):
    lb = label.Label(FONT, text="", color=color, scale=scale,
                     anchor_point=anchor, anchored_position=(x, y))
    ets_group.append(lb)
    return lb

# Row 1: speed + km/h (centred left of the sign) | speed-limit sign | cruise | pedals | gear
LIMIT_X, LIMIT_R = 128, 18
SPEED_CENTRE = (LIMIT_X - LIMIT_R) // 2          # halfway between screen edge and sign
speed_txt = make_text("speed", 3, 0xFFFFFF, 0, ROW1 + 12, fallback_scale=3)
kmh_txt = make_text("small", 4, 0xB3B3B3, 0, ROW1 + 12)
kmh_txt.text = "km/h"
ets_group.append(speed_txt.group)
ets_group.append(kmh_txt.group)


def place_speed():
    """Centre the number + 'km/h' together; called when the digit count changes."""
    gap = 3
    total = speed_txt.width + gap + kmh_txt.width
    left = SPEED_CENTRE - total // 2
    speed_txt.move(left)
    kmh_txt.move(left + speed_txt.width + gap)


limit_group = displayio.Group()
limit_group.append(vectorio.Circle(pixel_shader=solid(RED), radius=LIMIT_R, x=LIMIT_X, y=ROW1))
limit_group.append(vectorio.Circle(pixel_shader=solid(0xFFFFFF), radius=LIMIT_R - 4, x=LIMIT_X, y=ROW1))
limit_txt = make_text("sign", 3, 0x000000, LIMIT_X, ROW1 + 4, align="centre", bg=0xFFFFFF)
limit_group.append(limit_txt.group)
ets_group.append(limit_group)

cruise_txt = make_text("mid", 6, DIM, 151, ROW1 + 6, fallback_scale=2)
ets_group.append(cruise_txt.group)
gear_txt = make_text("gear", 3, 0xFFFFFF, WIDTH - 8, ROW1 + 10, align="right", fallback_scale=3)
ets_group.append(gear_txt.group)

# Pedal visualiser: two vertical bars (throttle green, brake red), fill from bottom
PEDAL_H, PEDAL_W, PEDAL_TOP = 34, 8, ROW1 - 17
PEDAL_BOTTOM = PEDAL_TOP + PEDAL_H
pedal_bars = []
for px, color in ((230, GREEN), (242, RED)):
    ets_group.append(vectorio.Rectangle(pixel_shader=solid(DIM), width=PEDAL_W,
                                        height=PEDAL_H, x=px, y=PEDAL_TOP))
    bar = vectorio.Rectangle(pixel_shader=solid(color), width=PEDAL_W, height=1,
                             x=px, y=PEDAL_BOTTOM - 1)
    ets_group.append(bar)
    pedal_bars.append(bar)


def set_pedal(bar, percent):
    h = max(1, PEDAL_H * max(0, min(100, percent)) // 100)
    if bar.height != h:
        bar.height = h
        bar.y = PEDAL_BOTTOM - h

# Row 2: < blinker | fuel bar + % | lights | park brake | nav distance | blinker >
blink_l_pal, blink_r_pal = solid(DIM), solid(DIM)
ets_group.append(vectorio.Polygon(pixel_shader=blink_l_pal,
                                  points=[(8, ROW2), (20, ROW2 - 8), (20, ROW2 + 8)]))
ets_group.append(vectorio.Polygon(pixel_shader=blink_r_pal,
                                  points=[(WIDTH - 8, ROW2), (WIDTH - 20, ROW2 - 8),
                                          (WIDTH - 20, ROW2 + 8)]))

FUEL_X, FUEL_W = 28, 50
ets_group.append(vectorio.Rectangle(pixel_shader=solid(DIM), width=FUEL_W, height=6,
                                    x=FUEL_X, y=ROW2 - 3))
fuel_pal = solid(GREEN)
fuel_bar = vectorio.Rectangle(pixel_shader=fuel_pal, width=1, height=6,
                              x=FUEL_X, y=ROW2 - 3)
ets_group.append(fuel_bar)
fuel_lbl = _lbl(FUEL_X + FUEL_W + 4, ROW2)
park_lbl = _lbl(158, ROW2, color=RED)

# Wiper icon (15x10 pixels): windscreen outline with a blade. Blue = on, dim = off.
WIPER_ICON = (
    "...XXXXXXXXX...",
    "..X.........X..",
    ".X...........X.",
    "X..X..........X",
    "X...X.........X",
    "X....X........X",
    "X.....X.......X",
    "X......X......X",
    ".X.....XX....X.",
    "..XXXXXXXXXXX..",
)
wiper_bmp = displayio.Bitmap(15, 10, 2)
for _y, _row in enumerate(WIPER_ICON):
    for _x, _c in enumerate(_row):
        if _c == "X":
            wiper_bmp[_x, _y] = 1
wiper_pal = displayio.Palette(2)
wiper_pal.make_transparent(0)
wiper_pal[1] = DIM
wiper_tg = displayio.TileGrid(wiper_bmp, pixel_shader=wiper_pal, x=136, y=ROW2 - 5)
wiper_tg.hidden = True                         # only shown while the wipers are on
ets_group.append(wiper_tg)

# Headlight symbols (18x13): standard low beam (green, beams angled down) and
# high beam (blue, straight beams). Only the active one is shown; none when off.
LOW_BEAM_ICON = (
    "......XX..XX......",
    "....XX....X.XXX...",
    "..XX......X....X..",
    "XX....XX..X.....X.",
    "....XX....X......X",
    "..XX......X......X",
    "XX....XX..X......X",
    "....XX....X......X",
    "..XX......X......X",
    "XX....XX..X.....X.",
    "....XX....X....X..",
    "..XX......X.XXX...",
    "XX........XX......",
)
HIGH_BEAM_ICON = (
    "XXXXXXXX..XX......",
    "..........X.XXX...",
    "..........X....X..",
    "XXXXXXXX..X.....X.",
    "..........X......X",
    "..........X......X",
    "XXXXXXXX..X......X",
    "..........X......X",
    "..........X......X",
    "XXXXXXXX..X.....X.",
    "..........X....X..",
    "..........X.XXX...",
    "XXXXXXXX..XX......",
)


def icon_tilegrid(rows, color, x, y):
    bmp = displayio.Bitmap(len(rows[0]), len(rows), 2)
    for _y, _row in enumerate(rows):
        for _x, _c in enumerate(_row):
            if _c == "X":
                bmp[_x, _y] = 1
    pal = displayio.Palette(2)
    pal.make_transparent(0)
    pal[1] = color
    tg = displayio.TileGrid(bmp, pixel_shader=pal, x=x, y=y)
    tg.hidden = True
    ets_group.append(tg)
    return tg


low_beam_tg = icon_tilegrid(LOW_BEAM_ICON, GREEN, 110, ROW2 - 6)
high_beam_tg = icon_tilegrid(HIGH_BEAM_ICON, BLUE, 110, ROW2 - 6)

# RPM bar between the two rows: green, amber above 75 %, red above 90 % of max
# The bar stops short of the right edge; the RPM number sits after it, under the gear.
RPM_NUM_W = 8 * 6                               # room for "2500 rpm" in the built-in font
RPM_X, RPM_Y, RPM_H = 8, ROW1 + 20, 4
RPM_W = (WIDTH - 8) - RPM_NUM_W - 6 - RPM_X
rpm_lbl = _lbl(WIDTH - 8, RPM_Y + RPM_H // 2, anchor=(1.0, 0.5))
ets_group.append(vectorio.Rectangle(pixel_shader=solid(DIM), width=RPM_W, height=RPM_H,
                                    x=RPM_X, y=RPM_Y))
rpm_pal = solid(GREEN)
rpm_bar = vectorio.Rectangle(pixel_shader=rpm_pal, width=1, height=RPM_H, x=RPM_X, y=RPM_Y)
ets_group.append(rpm_bar)
# Trip panel under the album name (text column, next to the art)
TRIP1_Y, TRIP2_Y = 106, 122                    # bottom line level with the bottom of the art
trip1_lbl = _lbl(TX, TRIP1_Y, color=0xB3B3B3)
trip2_lbl = _lbl(TX, TRIP2_Y, color=0x808080)


def hm(minutes):
    h, m = divmod(max(0, int(minutes)), 60)
    return "%dh %02dm" % (h, m) if h else "%dm" % m

ets = {}
ets_t = -100.0
ETS_TIMEOUT = 3


def set_color(lbl, color):
    if lbl.color != color:
        lbl.color = color


def update_ets(now):
    show = (now - ets_t) < ETS_TIMEOUT
    if ets_group.hidden == show:
        ets_group.hidden = not show
    if not show:
        return

    spd = str(ets.get("spd", 0))
    if len(spd) != len(speed_txt.text or ""):
        speed_txt.text = spd
        place_speed()                                  # re-centre when digit count changes
    else:
        speed_txt.text = spd
    speed_txt.color = 0x808080 if ets.get("p") else 0xFFFFFF

    lim = ets.get("lim", 0)
    limit_group.hidden = lim == 0
    limit_txt.text = str(lim) if lim else ""

    if ets.get("cc"):
        cruise_txt.text = "CC %d" % ets.get("ccs", 0)
        cruise_txt.color = GREEN
    else:
        cruise_txt.text = "CC"
        cruise_txt.color = DIM

    # Pedals: from the game for now. Once the Hall sensors are read in this
    # file, call set_pedal() with your own readings instead (no PC lag).
    set_pedal(pedal_bars[0], ets.get("thr", 0))
    set_pedal(pedal_bars[1], ets.get("brk", 0))

    gs = ets.get("gs")                                # gear text as the game shows it
    if gs is None:                                    # older bridge: build it here
        g = ets.get("gear", 0)
        gs = "N" if g == 0 else ("R%d" % -g if g < 0 else str(g))
    gear_txt.text = gs


    blink_on = (now % 0.8) < 0.4                     # blink locally, smoothly
    hz = ets.get("hz")
    blink_l_pal[0] = YELLOW if (blink_on and (ets.get("bl") or hz)) else DIM
    blink_r_pal[0] = YELLOW if (blink_on and (ets.get("br") or hz)) else DIM

    fuel = max(0, min(100, ets.get("fuel", 0)))
    fuel_bar.width = max(1, FUEL_W * fuel // 100)
    fuel_pal[0] = RED if fuel <= 10 else (AMBER if fuel <= 25 else GREEN)
    set_text(fuel_lbl, "%d%%" % fuel)

    high = bool(ets.get("high"))
    high_beam_tg.hidden = not high                     # high beam wins if both are on
    low_beam_tg.hidden = high or not ets.get("low")

    rpm, rpmx = ets.get("rpm", 0), ets.get("rpmx", 0) or 2500
    frac = min(1.0, rpm / rpmx)
    rpm_bar.width = max(1, int(RPM_W * frac))
    rpm_pal[0] = RED if frac > 0.9 else (AMBER if frac > 0.75 else GREEN)
    set_text(rpm_lbl, "%d rpm" % rpm)

    wiper_tg.hidden = not ets.get("wip")
    wiper_pal[1] = WIPER_ON

    set_text(park_lbl, "PARK" if ets.get("pb") else "")

    nav, eta = ets.get("nav", 0), ets.get("eta", 0)
    set_text(trip1_lbl, ("%d km left   ETA %s" % (nav, hm(eta))) if nav else "")
    parts = []
    due = ets.get("due", 0)
    if due > 0:
        parts.append("Due %s" % hm(due))
    elif due < 0:
        parts.append("Late %s" % hm(-due))
    if ets.get("rest"):
        parts.append("Rest %s" % hm(ets["rest"]))
    set_text(trip2_lbl, "    ".join(parts))


# ----------------------------------------------------- optional media keys ---
# Buttons to GND on GP2/GP3/GP4: previous / play-pause / next (normal media keys).
media_keys = keypad.Keys((board.GP2, board.GP3, board.GP4),
                         value_when_pressed=False, pull=True)
MEDIA_CODES = (ConsumerControlCode.SCAN_PREVIOUS_TRACK,
               ConsumerControlCode.PLAY_PAUSE,
               ConsumerControlCode.SCAN_NEXT_TRACK)
cc = ConsumerControl(usb_hid.devices)

# ------------------------------------------------------------------ state ---
ser = usb_cdc.data                # None if boot.py hasn't been applied
if ser is not None:
    ser.timeout = 0               # never block the main loop
    ser.write_timeout = 0.1       # don't hang if the PC isn't reading
    try:
        ser.write(b"READY\n")
    except Exception:
        pass

media = {"t": "", "a": "", "al": "", "pos": 0, "dur": 0, "play": False}
track_key = None
pos_at = time.monotonic()
last_msg = 0.0
TIMEOUT = 10

linebuf = b""
img = None                        # current incoming image: dict, or None
scratch = bytearray(256)          # used to discard an image we can't store


def set_text(lbl, text):
    if lbl.text != text:
        lbl.text = text


def fmt(sec):
    sec = max(0, int(sec))
    return "%d:%02d" % (sec // 60, sec % 60)


# ------------------------------------------------------- incoming messages ---
def handle_json(line):
    global track_key, pos_at, last_msg
    global ets_t
    try:
        msg = json.loads(line)
    except ValueError:
        return
    if "ets" in msg:                  # ETS2 dashboard data
        ets.update(msg["ets"] or {})
        ets_t = time.monotonic()
        return
    if "lt" in msg:
        sync_clock(msg.pop("lt"))
    media.update(msg)
    key = (media.get("t", ""), media.get("a", ""), media.get("al", ""))
    if key != track_key:          # new song: show ASCII text until images arrive
        track_key = key
        lines["title"].set_text(key[0] or "Nothing playing")
        lines["artist"].set_text(key[1])
        lines["album"].set_text(key[2])
        art_tg.hidden = True
    pos_at = time.monotonic()
    last_msg = pos_at


def apply_image(slot, w, h, bpp, data):
    if slot == "art":
        if (w, h, bpp) == (ART, ART, 16):
            bitmaptools.readinto(art_bmp, io.BytesIO(data), 16, element_size=2)
            art_tg.hidden = False
    elif slot in lines and bpp == 4:
        bmp = displayio.Bitmap(w, h, 16)
        bitmaptools.readinto(bmp, io.BytesIO(data), 4)
        lines[slot].set_image(bmp)


def start_image(header):
    """header: b'IMG slot w h bpp n'"""
    global img
    try:
        _, slot, w, h, bpp, n = header.split()
        slot = slot.decode()
        w, h, bpp, n = int(w), int(h), int(bpp), int(n)
    except ValueError:
        return
    gc.collect()
    try:
        buf = bytearray(n)
    except MemoryError:
        buf = None                # still have to read (and throw away) n bytes
    img = {"slot": slot, "w": w, "h": h, "bpp": bpp, "n": n, "got": 0,
           "buf": buf, "mv": memoryview(buf) if buf is not None else None,
           "t": time.monotonic()}


def feed_image(data):
    """Put already-read bytes into the incoming image. Returns leftover bytes."""
    take = min(len(data), img["n"] - img["got"])
    if img["buf"] is not None:
        img["mv"][img["got"]:img["got"] + take] = data[:take]
    img["got"] += take
    return data[take:]


def finish_image_if_done():
    global img
    if img is None or img["got"] < img["n"]:
        return
    done, img = img, None
    if done["buf"] is not None:
        try:
            apply_image(done["slot"], done["w"], done["h"], done["bpp"], done["buf"])
        except Exception as e:
            print("image error:", e)
    gc.collect()


def read_serial(now):
    global linebuf, img
    if ser is None:
        return

    if img is not None:                       # ---- receiving image bytes
        if not ser.in_waiting:
            if now - img["t"] > 3:            # sender stopped mid-image: give up
                img = None
                linebuf = b""
            return
        if img["buf"] is not None:
            k = ser.readinto(img["mv"][img["got"]:]) or 0
        else:
            want = min(len(scratch), img["n"] - img["got"])
            k = ser.readinto(memoryview(scratch)[:want]) or 0
        img["got"] += k
        img["t"] = now
        finish_image_if_done()
        return

    n = ser.in_waiting                        # ---- text lines
    if not n:
        return
    linebuf += ser.read(n)
    while img is None and b"\n" in linebuf:
        line, linebuf = linebuf.split(b"\n", 1)
        line = line.strip()
        if line.startswith(b"IMG "):
            start_image(line)
            if img is not None:
                linebuf = feed_image(linebuf)
                finish_image_if_done()
        elif line == b"HELLO":
            ser.write(b"PICO-MEDIA\n")
        elif line.startswith(b"{"):
            handle_json(line)
    if len(linebuf) > 4096:                   # garbage protection
        linebuf = b""


# ------------------------------------------------------------------ screen ---
def update_screen(now):
    connected = last_msg and (now - last_msg) < TIMEOUT
    if not connected:
        set_text(state_lbl, "Waiting for PC..." if ser else "boot.py not active")
        return

    pos = media["pos"] + ((now - pos_at) if media["play"] else 0)
    dur = media["dur"]
    if dur:
        pos = min(pos, dur)
        bar_fg.width = max(1, int(BAR_W * pos / dur))
        set_times(fmt(pos), fmt(dur))
    else:
        bar_fg.width = 1
        set_times("", "")
    set_text(state_lbl, "Playing" if media["play"] else "Paused")

    for ln in lines.values():
        ln.scroll(now)


# -------------------------------------------------------------- main loop ---
last_frame = 0.0
while True:
    now = time.monotonic()

    read_serial(now)

    ev = media_keys.events.get()
    if ev and ev.pressed:
        cc.send(MEDIA_CODES[ev.key_number])

    # >>> your pedal / switch scanning goes here (keep it non-blocking) <<<

    if now - last_frame >= 0.05:      # ~20 fps
        update_screen(now)
        update_ets(now)
        update_clock()
        last_frame = now
