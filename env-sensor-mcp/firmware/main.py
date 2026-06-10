# MicroPython firmware for M5StickC Plus2 + ENV III (SHT30 + QMP6988).
#
# Embodied Claude "skin": exposes ambient temperature / humidity / pressure
# over HTTP so the agent can pull them like any other sense, AND shows them
# on the built-in LCD so the device isn't a dark, lonely brick.
#
#   GET /env   -> {"temp_c":..,"humidity":..,"pressure_hpa":..,"rssi":..,"uptime_s":..}
#   GET /      -> same JSON (convenience)
#   GET /health-> {"ok":true}
#
# Sensors hang off the Grove I2C port (SDA=G32, SCL=G33).
# LCD is the M5StickC Plus2 ST7789 (CS=5, RST=12, DC=14, MOSI=15, SCLK=13, BL=27).
# Secrets and static-IP config live in config.py (not in git).

import time
import struct
import json
import random
import network
import socket
from machine import I2C, Pin, SPI

import config

I2C_SDA = 32
I2C_SCL = 33
SHT30_ADDR = 0x44
QMP6988_ADDR = 0x70

_BOOT_MS = time.ticks_ms()

# Keep the device powered (M5StickC Plus2 power-hold pin).
Pin(4, Pin.OUT, value=1)

i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=100000)


# ---------------------------------------------------------------- QMP6988
class QMP6988:
    """Pressure / temperature sensor with the datasheet compensation."""

    def __init__(self, bus, addr=QMP6988_ADDR):
        self.bus = bus
        self.addr = addr
        # temp x4, press x8, normal mode (0b011_100_11), IIR off.
        bus.writeto_mem(addr, 0xF4, bytes([0x73]))
        bus.writeto_mem(addr, 0xF1, bytes([0x00]))
        time.sleep_ms(50)
        self._read_calibration()

    def _read_calibration(self):
        c = self.bus.readfrom_mem(self.addr, 0xA0, 25)

        def s16(o):
            return struct.unpack(">h", c[o : o + 2])[0]

        b00_ = s16(0)
        bt1, bt2, bp1 = s16(2), s16(4), s16(6)
        b11, bp2, b12 = s16(8), s16(10), s16(12)
        b21, bp3 = s16(14), s16(16)
        a0_, a1, a2 = s16(18), s16(20), s16(22)
        ex = c[24]

        # a0 / b00 are 20-bit: 16 high bits + a nibble from the extension byte.
        self.a0 = ((a0_ << 4) | (ex & 0x0F)) / 16.0
        self.b00 = ((b00_ << 4) | ((ex >> 4) & 0x0F)) / 16.0
        # coeff = offset + scale * (raw / 32767)
        self.a1 = -6.30e-3 + 4.30e-4 * a1 / 32767
        self.a2 = -1.90e-11 + 1.20e-10 * a2 / 32767
        self.bt1 = 1.00e-1 + 9.10e-2 * bt1 / 32767
        self.bt2 = 1.20e-8 + 1.20e-6 * bt2 / 32767
        self.bp1 = 3.30e-2 + 1.90e-2 * bp1 / 32767
        self.b11 = 2.10e-7 + 1.40e-7 * b11 / 32767
        self.bp2 = -6.30e-10 + 3.50e-10 * bp2 / 32767
        self.b12 = 2.90e-13 + 7.60e-13 * b12 / 32767
        self.b21 = 2.10e-15 + 1.20e-14 * b21 / 32767
        self.bp3 = 1.30e-16 + 7.90e-17 * bp3 / 32767

    def read(self):
        """Return (temp_c, pressure_pa)."""
        d = self.bus.readfrom_mem(self.addr, 0xF7, 6)
        rp = (d[0] << 16) | (d[1] << 8) | d[2]
        rt = (d[3] << 16) | (d[4] << 8) | d[5]
        dt = rt - (1 << 23)
        dp = rp - (1 << 23)
        tr = self.a0 + self.a1 * dt + self.a2 * dt * dt
        pr = (
            self.b00
            + self.bt1 * tr
            + self.bp1 * dp
            + self.b11 * tr * dp
            + self.bt2 * tr * tr
            + self.bp2 * dp * dp
            + self.b12 * dp * tr * tr
            + self.b21 * dp * dp * tr
            + self.bp3 * dp * dp * dp
        )
        return tr / 256.0, pr


def read_sht30():
    """Return (temp_c, humidity_pct)."""
    i2c.writeto(SHT30_ADDR, b"\x24\x00")  # single-shot, high repeatability
    time.sleep_ms(20)
    d = i2c.readfrom(SHT30_ADDR, 6)
    rt = (d[0] << 8) | d[1]
    rh = (d[3] << 8) | d[4]
    return -45 + 175 * rt / 65535, 100 * rh / 65535


qmp = QMP6988(i2c)


def read_all():
    t_sht, hum = read_sht30()
    t_qmp, press = qmp.read()
    return {
        "temp_c": round((t_sht + t_qmp) / 2.0, 2),
        "temp_sht_c": round(t_sht, 2),
        "temp_qmp_c": round(t_qmp, 2),
        "humidity": round(hum, 1),
        "pressure_hpa": round(press / 100.0, 2),
        "rssi": _wlan.status("rssi") if _wlan and _wlan.isconnected() else None,
        "uptime_s": time.ticks_diff(time.ticks_ms(), _BOOT_MS) // 1000,
        "last_shake_g": round(_last_shake_g, 2),
        "last_shake_ago_s": (
            time.ticks_diff(time.ticks_ms(), _last_shake_ms) // 1000
            if _last_shake_ms else None
        ),
        "shake_count": _shake_count,
    }


# ---------------------------------------------------------------- LCD / face
_tft = None
_big = None
_small = None
_blink = False  # toggles each refresh so the face blinks

# Face geometry (left side of the 240x135 landscape screen).
_FX, _FY, _FR = 66, 64, 56
_SKIN = None
_DARK = None
_BLUSH = None
_SWEAT = None


def _fill_circle(cx, cy, r, color):
    for dy in range(-r, r + 1):
        dx = int((r * r - dy * dy) ** 0.5)
        _tft.hline(cx - dx, cy + dy, 2 * dx + 1, color)


def _smile(cx, cy, w, depth, color, up=True, thick=3):
    # parabola: smile (U, corners up) when up=True, frown when up=False
    for dx in range(-w, w + 1):
        f = (dx * dx) / (w * w)
        y = cy + int(depth * (1 - f)) if up else cy - int(depth * (1 - f))
        for k in range(thick):
            _tft.pixel(cx + dx, y + k, color)


def _mood(data):
    t, h = data["temp_c"], data["humidity"]
    if t <= 17:
        return "cold"
    if t >= 30:
        return "hot"
    if t >= 27.5 or h >= 72:
        return "warm"
    return "comfy"


def _draw_face(data):
    import st7789py as st7789
    global _blink
    cx, cy, r = _FX, _FY, _FR
    mood = _mood(data)

    # clear the face area, draw the round skin
    _tft.fill_rect(0, 0, 138, 135, st7789.BLACK)
    _fill_circle(cx, cy, r, _SKIN)

    ex, ey, er = 22, 16, 9  # eye offset / size
    # --- eyes ---
    if _blink:
        _tft.fill_rect(cx - ex - er, cy - ey, 2 * er, 3, _DARK)
        _tft.fill_rect(cx + ex - er, cy - ey, 2 * er, 3, _DARK)
    elif mood == "hot":  # >_<  scrunched
        for s in (-1, 1):
            bx = cx + s * ex
            _tft.line(bx - er, cy - ey - 5, bx + er, cy - ey, _DARK)
            _tft.line(bx - er, cy - ey + 5, bx + er, cy - ey, _DARK)
    else:
        _fill_circle(cx - ex, cy - ey, er, _DARK)
        _fill_circle(cx + ex, cy - ey, er, _DARK)
        # little catchlights
        _tft.fill_rect(cx - ex - 2, cy - ey - 4, 3, 3, st7789.WHITE)
        _tft.fill_rect(cx + ex - 2, cy - ey - 4, 3, 3, st7789.WHITE)

    # --- blush (comfy / warm) ---
    if mood in ("comfy", "warm"):
        _fill_circle(cx - 34, cy + 8, 7, _BLUSH)
        _fill_circle(cx + 34, cy + 8, 7, _BLUSH)

    # --- mouth ---
    my = cy + 22
    if mood == "hot":
        _fill_circle(cx, my, 9, _DARK)  # open "haa"
    elif mood == "cold":
        _tft.fill_rect(cx - 8, my, 16, 3, _DARK)  # tight straight mouth
    else:
        _smile(cx, my - 4, 16, 9, _DARK, up=True, thick=3)  # happy

    # --- sweat drop (warm / hot), shiver (cold) ---
    if mood in ("warm", "hot"):
        sx, sy = cx + r - 8, cy - 18
        _fill_circle(sx, sy + 4, 4, _SWEAT)
        _tft.line(sx, sy - 4, sx - 3, sy + 2, _SWEAT)
        _tft.line(sx, sy - 4, sx + 3, sy + 2, _SWEAT)
        if mood == "hot":
            _fill_circle(cx - r + 8, cy - 8, 3, _SWEAT)
    elif mood == "cold":
        for i in range(3):
            _tft.text(_small, "~", cx + r - 14 + i * 0, cy - 30 + i * 12,
                      st7789.CYAN, _SKIN)

    _blink = not _blink
    return mood


# 16x16 blocky face mask for the 8-bit style (Y = skin pixel).
_SKIN_MASK = (
    "....YYYYYYYY....",
    "..YYYYYYYYYYYY..",
    ".YYYYYYYYYYYYYY.",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    "YYYYYYYYYYYYYYYY",
    ".YYYYYYYYYYYYYY.",
    ".YYYYYYYYYYYYYY.",
    "..YYYYYYYYYYYY..",
    "....YYYYYYYY....",
)


def _draw_face_pixel(data):
    """8-bit / pixel-art face: a 16x16 sprite of 8px blocks, mood-reactive."""
    import st7789py as st7789
    global _blink
    S, x0, y0 = 8, 5, 3

    def px(c, r, color):
        _tft.fill_rect(x0 + c * S, y0 + r * S, S, S, color)

    mood = _mood(data)
    _tft.fill_rect(0, 0, 138, 135, st7789.BLACK)
    for r, row in enumerate(_SKIN_MASK):
        for c, ch in enumerate(row):
            if ch == "Y":
                px(c, r, _SKIN)

    # --- eyes (2x2 blocks at cols 4-5 / 10-11) ---
    if _blink:
        for ec in (4, 10):
            px(ec, 6, _DARK)
            px(ec + 1, 6, _DARK)
    elif mood == "hot":  # >_<
        px(4, 5, _DARK); px(5, 6, _DARK); px(4, 7, _DARK)
        px(11, 5, _DARK); px(10, 6, _DARK); px(11, 7, _DARK)
    else:
        for ec in (4, 10):
            px(ec, 5, _DARK); px(ec + 1, 5, _DARK)
            px(ec, 6, _DARK); px(ec + 1, 6, _DARK)
            _tft.fill_rect(x0 + ec * S, y0 + 5 * S, 3, 3, st7789.WHITE)  # catchlight

    # --- blush ---
    if mood in ("comfy", "warm"):
        px(2, 8, _BLUSH); px(3, 8, _BLUSH)
        px(12, 8, _BLUSH); px(13, 8, _BLUSH)

    # --- mouth ---
    if mood == "hot":
        for c in (7, 8):
            px(c, 9, _DARK); px(c, 10, _DARK); px(c, 11, _DARK)
    elif mood == "cold":
        for c in (6, 7, 8, 9):
            px(c, 10, _DARK)
    else:  # smile (U)
        px(5, 10, _DARK); px(10, 10, _DARK)
        for c in (6, 7, 8, 9):
            px(c, 11, _DARK)

    # --- sweat / shiver ---
    if mood in ("warm", "hot"):
        px(13, 2, _SWEAT)
        _tft.fill_rect(x0 + 13 * S + 1, y0 + 1 * S + 3, 5, 5, _SWEAT)
        if mood == "hot":
            px(2, 4, _SWEAT)

    _blink = not _blink
    return mood


def init_display():
    """Bring up the ST7789 and paint the static layout once."""
    global _tft, _big, _small, _SKIN, _DARK, _BLUSH, _SWEAT
    import st7789py as st7789
    import vga1_bold_16x32 as big
    import vga1_8x16 as small

    spi = SPI(1, baudrate=20000000, sck=Pin(13), mosi=Pin(15))
    _tft = st7789.ST7789(
        spi, 135, 240,
        cs=Pin(5, Pin.OUT), dc=Pin(14, Pin.OUT),
        reset=Pin(12, Pin.OUT), backlight=Pin(27, Pin.OUT),
        rotation=1,
    )
    _big, _small = big, small
    _SKIN = st7789.color565(255, 210, 90)   # warm emoji yellow
    _DARK = st7789.color565(60, 40, 20)      # eyes / mouth
    _BLUSH = st7789.color565(255, 130, 150)  # cheeks
    _SWEAT = st7789.color565(120, 200, 255)  # sweat drop
    _tft.fill(st7789.BLACK)
    _tft.text(_big, "SORA", 150, 8, st7789.CYAN, st7789.BLACK)


def update_display(data):
    """Redraw the face (mood from the air) + the live values on the right."""
    if _tft is None:
        return
    import st7789py as st7789

    try:
        if getattr(config, "FACE_STYLE", "pixel") == "pixel":
            _draw_face_pixel(data)
        else:
            _draw_face(data)
    except Exception as e:
        print("face draw error:", e)

    gray = st7789.color565(130, 130, 130)
    x = 150
    _tft.text(_small, "%5.1f C  " % data["temp_c"], x, 48, st7789.WHITE, st7789.BLACK)
    _tft.text(_small, "%5.1f %%  " % data["humidity"], x, 70, st7789.WHITE, st7789.BLACK)
    _tft.text(_small, "%6.1fhPa" % data["pressure_hpa"], x, 92, st7789.WHITE, st7789.BLACK)
    rssi = data.get("rssi")
    _tft.text(_small, "rssi%4d  " % (rssi if rssi is not None else 0),
              x, 116, gray, st7789.BLACK)


# ---------------------------------------------------------------- IMU / shake
_imu = None
IMU_ADDR = 0x68  # MPU6886 on the internal I2C bus (SDA=21, SCL=22)

# Last shake felt, so the host can surface it as a "touch" sense.
_last_shake_g = 0.0   # peak deviation from 1g during the most recent shake
_last_shake_ms = 0    # ticks_ms of the last moment motion was felt
_shake_count = 0      # total shakes since boot


def init_imu():
    global _imu
    _imu = I2C(1, scl=Pin(22), sda=Pin(21), freq=100000)
    _imu.writeto_mem(IMU_ADDR, 0x6B, bytes([0x00]))  # wake
    time.sleep_ms(10)
    _imu.writeto_mem(IMU_ADDR, 0x1C, bytes([0x08]))  # accel +-4g -> 8192 LSB/g


def _accel_mag():
    """Magnitude of acceleration in g. ~1.0 at rest, regardless of tilt."""
    d = _imu.readfrom_mem(IMU_ADDR, 0x3B, 6)
    ax = struct.unpack(">h", d[0:2])[0] / 8192.0
    ay = struct.unpack(">h", d[2:4])[0] / 8192.0
    az = struct.unpack(">h", d[4:6])[0] / 8192.0
    return (ax * ax + ay * ay + az * az) ** 0.5


# Shake reactions. Picked at random each jostle so you never know which
# face pops out — being shaken is play, not distress.
SHAKE_FACES = ("surprised", "dizzy", "squee", "laugh")


def _caret(bx, by, w, color):
    """A ∧ shape — closed happy/laughing eye."""
    for off in (-1, 0, 1):
        _tft.line(bx - w, by + off + 4, bx, by + off - 4, color)
        _tft.line(bx, by + off - 4, bx + w, by + off + 4, color)


def _sparkle(x, y, color):
    _tft.hline(x - 3, y, 7, color)
    _tft.vline(x, y - 3, 7, color)


def _draw_face_shake(kind):
    """Transient reaction face when jostled. kind in SHAKE_FACES."""
    import st7789py as st7789

    cx, cy, r = _FX, _FY, _FR
    ex, ey = 22, 14
    _tft.fill_rect(0, 0, 138, 135, st7789.BLACK)
    _fill_circle(cx, cy, r, _SKIN)

    if kind == "surprised":  # びっくり: wide eyes, open O, sweat
        for s in (-1, 1):
            _fill_circle(cx + s * ex, cy - ey, 13, st7789.WHITE)
            _fill_circle(cx + s * ex, cy - ey, 5, _DARK)
        _fill_circle(cx, cy + 20, 10, _DARK)
        for s in (-1, 1):
            _fill_circle(cx + s * (r - 6), cy - 22, 4, _SWEAT)

    elif kind == "dizzy":  # めまい: x_x eyes, wavy mouth
        for s in (-1, 1):
            bx, by = cx + s * ex, cy - ey
            for off in (-1, 0, 1):
                _tft.line(bx - 8, by - 8 + off, bx + 8, by + 8 + off, _DARK)
                _tft.line(bx - 8, by + 8 + off, bx + 8, by - 8 + off, _DARK)
        zig = (0, 3, 0, -3, 0, 3, 0, -3, 0)
        for i, dx in enumerate(range(-16, 17, 4)):
            _tft.fill_rect(cx + dx - 1, cy + 20 + zig[i], 4, 3, _DARK)

    elif kind == "squee":  # きゃーー: sparkly eyes, big smile, blush, stars
        for s in (-1, 1):
            _fill_circle(cx + s * ex, cy - ey, 11, st7789.WHITE)
            _fill_circle(cx + s * ex, cy - ey, 4, _DARK)
            _tft.fill_rect(cx + s * ex - 6, cy - ey - 6, 3, 3, st7789.WHITE)
        _fill_circle(cx - 34, cy + 8, 7, _BLUSH)
        _fill_circle(cx + 34, cy + 8, 7, _BLUSH)
        _smile(cx, cy + 14, 16, 11, _DARK, up=True, thick=4)  # big grin
        _sparkle(cx - r + 4, cy - 30, st7789.CYAN)
        _sparkle(cx + r - 4, cy - 26, st7789.CYAN)
        _sparkle(cx + r + 2, cy + 4, st7789.WHITE)

    else:  # laugh / おおわらい: ∧∧ eyes, big open mouth, tears of joy
        _caret(cx - ex, cy - ey, 9, _DARK)
        _caret(cx + ex, cy - ey, 9, _DARK)
        _fill_circle(cx, cy + 18, 11, _DARK)             # big "ahaha" mouth
        _tft.fill_rect(cx - 8, cy + 12, 16, 3, _SKIN)    # upper lip line
        for s in (-1, 1):                                 # tears of joy
            _fill_circle(cx + s * (ex + 16), cy - ey + 4, 4, _SWEAT)


# ---------------------------------------------------------------- WiFi
_wlan = None


def wifi_connect():
    global _wlan
    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(True)
    if config.USE_STATIC_IP:
        _wlan.ifconfig((config.IP, config.NETMASK, config.GATEWAY, config.DNS))
    if not _wlan.isconnected():
        _wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        for _ in range(40):  # ~20s
            if _wlan.isconnected():
                break
            time.sleep_ms(500)
    return _wlan.isconnected()


# ---------------------------------------------------------------- HTTP
def _respond(cl, status, body, ctype="application/json"):
    cl.send(
        "HTTP/1.1 %s\r\nContent-Type: %s\r\nConnection: close\r\n"
        "Access-Control-Allow-Origin: *\r\nContent-Length: %d\r\n\r\n"
        % (status, ctype, len(body))
    )
    cl.send(body)


def serve():
    global _last_shake_g, _last_shake_ms, _shake_count
    addr = socket.getaddrinfo("0.0.0.0", config.HTTP_PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    s.settimeout(0.08)  # poll ~12Hz so we can feel shakes between requests
    print("env-sensor serving on", config.HTTP_PORT)
    last_draw = 0
    shaken_until = 0
    shaking = False
    last_shake_draw = 0
    shake_peak = 0.0
    while True:
        # --- serve one HTTP request if a client is waiting ---
        try:
            cl, _ = s.accept()
            cl.settimeout(3)
            try:
                req = cl.recv(256)
                path = b"/"
                if req:
                    parts = req.split(b" ")
                    if len(parts) > 1:
                        path = parts[1]
                if path.startswith(b"/health"):
                    _respond(cl, "200 OK", b'{"ok":true}')
                elif path.startswith(b"/env") or path == b"/":
                    body = json.dumps(read_all()).encode()
                    _respond(cl, "200 OK", body)
                else:
                    _respond(cl, "404 Not Found", b'{"error":"not found"}')
            except Exception as e:  # one bad request must not kill the server
                try:
                    _respond(cl, "500 Internal Server Error",
                             json.dumps({"error": str(e)}).encode())
                except Exception:
                    pass
            finally:
                cl.close()
        except OSError:
            pass  # accept() timed out -> normal, fall through
        except Exception as e:
            print("accept error:", e)
            time.sleep_ms(200)

        now = time.ticks_ms()

        # --- feel for shakes: random face from the 4, reshuffled while shaken ---
        try:
            if _imu is not None:
                dev = abs(_accel_mag() - 1.0)
                if dev > 0.28:
                    # track the running peak; expose it live for the touch sense
                    shake_peak = dev if not shaking else max(shake_peak, dev)
                    _last_shake_g = shake_peak
                    _last_shake_ms = now
                    if not shaking or time.ticks_diff(now, last_shake_draw) > 400:
                        _draw_face_shake(random.choice(SHAKE_FACES))
                        last_shake_draw = now
                    shaking = True
                    shaken_until = time.ticks_add(now, 1200)
                elif shaking and time.ticks_diff(now, shaken_until) >= 0:
                    # shake finished: commit the event AFTER the peak settled,
                    # so consumers that trigger on shake_count read the full peak.
                    shaking = False
                    _shake_count += 1
                    update_display(read_all())
                    last_draw = now
        except Exception as e:
            print("imu error:", e)

        # --- periodic LCD refresh (skipped while reacting to a shake) ---
        try:
            if not shaking and time.ticks_diff(now, last_draw) > 4000:
                update_display(read_all())
                last_draw = time.ticks_ms()
        except Exception as e:
            print("display error:", e)


def main():
    try:
        init_display()
    except Exception as e:
        print("display init failed:", e)
    try:
        init_imu()
    except Exception as e:
        print("imu init failed:", e)
    ok = wifi_connect()
    print("wifi:", _wlan.ifconfig() if ok else "FAILED")
    try:
        update_display(read_all())
    except Exception as e:
        print("first draw failed:", e)
    while True:
        try:
            serve()
        except Exception as e:
            print("serve crashed, restarting:", e)
            if not _wlan.isconnected():
                wifi_connect()
            time.sleep_ms(500)


main()
