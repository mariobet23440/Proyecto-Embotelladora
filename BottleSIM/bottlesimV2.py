"""
BottleSIM - simulador gráfico de la línea de embotellado (Capstone IE3037)
Diseñado por ℳβ · Implementado por: Claude AI

  pip install customtkinter python-snap7
  python bottlesim.py                                      # modo demo: clic en los LEDs Q fuerza salidas
  python bottlesim.py --ip 192.168.0.1 --rack 0 --slot 2   # conectado al PLC (o usa el botón «Conexión PLC…»)

E/S: el simulador ESCRIBE %IB8 y %IB9 (área PE) y LEE %QB12 (área PA).
"""
import argparse, math, random, threading, time
import tkinter as tk
import customtkinter as ctk

try:
    import snap7
    try:
        from snap7.type import Area as _A        # python-snap7 2.x
    except ImportError:
        from snap7.types import Areas as _A      # python-snap7 1.x
    PE, PA = _A.PE, _A.PA
except ImportError:
    snap7 = None

# ---------------- Geometría y parámetros ----------------
SEG = [((150, 90), (150, 470)), ((150, 470), (750, 470)), ((750, 470), (750, 90))]
LENS = [380, 600, 380]
L = sum(LENS)            # 1360 px de recorrido
STEP = L / 20            # 68 px = 1 posición del shift register = 1 pulso del encoder
BELT_W = 56
V_BASE = 200             # px/s al 100 % de velocidad
T_LLENADO = 3.0          # s para que una botella normal llegue a 100 % con la válvula abierta
NIVEL_OK = 90.0          # % mínimo para que el sensor de nivel dé 1 (llenado correcto)
K_FILL, K_CAP, K_LS, K_REJ = (3, 4, 5), 15, (10, 11, 12), 17   # posiciones del registro

IN = [("I8.0 Marcha", 0, 0), ("I8.1 Paro (NC)", 0, 1), ("I8.4 E-Stop (NC)", 0, 4), ("I9.0 Botella", 1, 0),
      ("I9.1 Encoder", 1, 1), ("I9.5 LS-1", 1, 5), ("I9.6 LS-2", 1, 6), ("I9.7 LS-3", 1, 7)]
OUT = ["Q12.0 Banda", "Q12.1 E-1", "Q12.2 E-2", "Q12.3 E-3", "Q12.4 Rechazo", "Q12.5 Marcha", "Q12.6 Alarma T", "Q12.7 Lote OK"]


def pos(s):
    """Punto (x, y) y dirección unitaria sobre la U para un recorrido s."""
    s = max(0.0, min(float(L), s))
    for (a, b), n in zip(SEG, LENS):
        if s <= n:
            t = s / n
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, ((b[0] - a[0]) / n, (b[1] - a[1]) / n)
        s -= n


class Bottle:
    def __init__(self, s, defect):
        self.s, self.fill, self.cap, self.xoff, self.push = s, 0.0, False, 0.0, False
        self.k = random.uniform(0.15, 0.6) if defect else random.uniform(1.0, 1.15)  # velocidad de llenado relativa


_s = lambda v: (v.decode(errors="ignore") if isinstance(v, (bytes, bytearray)) else str(v)).strip("\x00 ")


def probe(ip, rack, slot):
    """Verifica la conexión con un cliente temporal. Devuelve [(ok, texto), ...]."""
    if snap7 is None:
        return [(False, "python-snap7 no está instalado:  pip install python-snap7")]
    cli, out = snap7.client.Client(), []
    try:
        t0 = time.perf_counter()
        cli.connect(ip, rack, slot)
        out.append((True, f"Conexión con {ip} (rack {rack}, slot {slot}): {(time.perf_counter() - t0) * 1000:.0f} ms"))
    except Exception as e:
        return [(False, f"No se pudo conectar: {e}"),
                (None, "Revisa IP, rack/slot y que PLCSIM (o NetToPLCSim) esté activo y en RUN.")]

    def step(label, fn):
        try:
            out.append((True, f"{label}: {fn()}"))
        except Exception as e:
            out.append((False, f"{label}: {e}"))

    step("Módulo de CPU", lambda: _s(cli.get_cpu_info().ModuleTypeName))
    step("Estado de CPU", lambda: _s(cli.get_cpu_state()))
    step("Lectura %QB12 (salidas)", lambda: f"0b{cli.read_area(PA, 0, 12, 1)[0]:08b}")
    step("Lectura %IB8-%IB9 (entradas)", lambda: " ".join(f"0b{v:08b}" for v in cli.read_area(PE, 0, 8, 2)))
    step("Escritura %IB8-%IB9 (estado en reposo)", lambda: (cli.write_area(PE, 0, 8, bytearray([0b00010010, 0])), "OK")[1])
    try:
        cli.disconnect()
    except Exception:
        pass
    return out


class PLCLink(threading.Thread):
    """Hilo que escribe las entradas y lee las salidas del PLC. Desconectado = modo demo."""
    def __init__(self):
        super().__init__(daemon=True)
        self.ip, self.rack, self.slot = "192.168.0.1", 0, 2
        self.enabled, self.connected, self.reset = False, False, False
        self.inputs, self.q, self.cycle_ms = bytearray(2), 0, 0.0
        self.status = "Modo demo (sin PLC)"

    @property
    def demo(self):
        return not self.enabled

    def connect(self, ip, rack, slot):
        if snap7 is None:
            self.status = "Falta python-snap7 (pip install python-snap7)"
            return False
        self.ip, self.rack, self.slot = ip, rack, slot
        self.status, self.reset, self.enabled = f"Conectando a {ip}...", True, True
        return True

    def disconnect(self):
        self.enabled = False

    def run(self):
        cli = None
        while True:
            if not self.enabled or self.reset:
                if cli is not None:
                    try:
                        cli.disconnect()
                    except Exception:
                        pass
                    cli = None
                self.reset = False
                if not self.enabled:
                    if self.connected:
                        self.connected, self.q, self.status = False, 0, "Modo demo (sin PLC)"
                    time.sleep(0.1)
                    continue
            try:
                if cli is None:
                    cli = snap7.client.Client()
                if not cli.get_connected():
                    cli.connect(self.ip, self.rack, self.slot)
                    self.connected, self.status = True, f"PLC conectado ({self.ip})"
                t0 = time.perf_counter()
                cli.write_area(PE, 0, 8, bytearray(self.inputs))   # %IB8, %IB9
                self.q = cli.read_area(PA, 0, 12, 1)[0]            # %QB12
                self.cycle_ms = (time.perf_counter() - t0) * 1000
                time.sleep(0.03)
            except Exception as e:
                self.connected, self.status = False, f"Error: {str(e)[:48]}"
                try:
                    cli.disconnect()
                except Exception:
                    pass
                cli = None
                time.sleep(1.0)


class ConnWindow(ctk.CTkToplevel):
    """Ventana para establecer y verificar la conexión con el PLC."""
    def __init__(self, app):
        super().__init__(app)
        self.link, self.closed = app.link, False
        self.title("Conexión con el PLC"); self.geometry("470x500"); self.resizable(False, False)
        self.transient(app); self.protocol("WM_DELETE_WINDOW", self._close)
        ctk.CTkLabel(self, text="Conexión con el PLC", font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=18, pady=(14, 0))
        ctk.CTkLabel(self, justify="left", anchor="w", text_color="#8b949e", wraplength=430,
                     text="S7-300 (CPU 315): rack 0, slot 2. Con PLCSIM V5.4 usa la IP que configuraste en NetToPLCSim."
                     ).pack(anchor="w", padx=18, pady=(2, 10))
        form = ctk.CTkFrame(self, fg_color="transparent"); form.pack(fill="x", padx=18)
        self.ip, self.rack, self.slot = (ctk.StringVar(value=str(v)) for v in (self.link.ip, self.link.rack, self.link.slot))
        for r, (name, var, w) in enumerate((("IP", self.ip, 170), ("Rack", self.rack, 70), ("Slot", self.slot, 70))):
            ctk.CTkLabel(form, text=name, width=50, anchor="w").grid(row=r, column=0, pady=4)
            ctk.CTkEntry(form, textvariable=var, width=w).grid(row=r, column=1, sticky="w", pady=4)
        row = ctk.CTkFrame(self, fg_color="transparent"); row.pack(fill="x", padx=18, pady=12)
        self.btn_test = ctk.CTkButton(row, text="Probar conexión", width=130, command=self._probe); self.btn_test.pack(side="left")
        ctk.CTkButton(row, text="Conectar", width=100, fg_color="#2e7d32", hover_color="#388e3c", command=self._connect).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Desconectar", width=100, fg_color="#b26a00", hover_color="#c77800", command=self._disconnect).pack(side="left")
        self.lbl = ctk.CTkLabel(self, text="", anchor="w", font=("Segoe UI", 13, "bold")); self.lbl.pack(fill="x", padx=18)
        self.log = ctk.CTkTextbox(self, height=230, font=("Consolas", 12), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=18, pady=(6, 16))
        self._log([(None, "«Probar conexión» verifica sin iniciar el enlace; «Conectar» enlaza BottleSIM con el PLC.")])
        self._refresh(); self.after(100, self.lift)

    def _close(self):
        self.closed = True; self.destroy()

    def _log(self, items):
        mark = {True: "✔ ", False: "✘ ", None: "• "}
        self.log.configure(state="normal"); self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(mark[ok] + t for ok, t in items)); self.log.configure(state="disabled")

    def _args(self):
        try:
            ip = self.ip.get().strip()
            if not ip:
                raise ValueError("falta la IP")
            return ip, int(self.rack.get()), int(self.slot.get())
        except ValueError as e:
            self._log([(False, f"Datos inválidos: {e}")]); return None

    def _probe(self):
        a = self._args()
        if a is None:
            return
        if self.link.connected:
            self._log([(True, f"Ya hay una conexión activa con {self.link.ip}"),
                       (True, f"Ciclo de lectura/escritura de E/S: {self.link.cycle_ms:.1f} ms")]); return
        self.btn_test.configure(state="disabled"); self._log([(None, "Probando conexión...")])
        res = []
        t = threading.Thread(target=lambda: res.append(probe(*a)), daemon=True); t.start()
        self.after(150, lambda: self._wait(t, res))

    def _wait(self, t, res):
        if self.closed:
            return
        if t.is_alive():
            self.after(150, lambda: self._wait(t, res)); return
        self.btn_test.configure(state="normal"); self._log(res[0])

    def _connect(self):
        a = self._args()
        if a is None:
            return
        self._log([(None, f"Conectando a {a[0]} (rack {a[1]}, slot {a[2]})...")] if self.link.connect(*a) else [(False, self.link.status)])

    def _disconnect(self):
        self.link.disconnect(); self._log([(None, "Desconectado. BottleSIM vuelve al modo demo.")])

    def _refresh(self):
        if self.closed:
            return
        l = self.link
        if l.connected:
            txt, col = f"● Conectado a {l.ip} · ciclo de E/S {l.cycle_ms:.0f} ms", "#37d67a"
        elif l.enabled:
            txt, col = f"● {l.status}", "#e3b341"
        else:
            txt, col = "● Desconectado (modo demo)", "#8b949e"
        self.lbl.configure(text=txt, text_color=col)
        self.after(300, self._refresh)


class App(ctk.CTk):
    def __init__(self, link):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("BottleSIM"); self.geometry("1240x700"); self.resizable(False, False)
        self.link, self.conn_win = link, None
        self.travel, self.last_idx, self.feeding = 0.0, 0, False
        self.bottles, self.rej_vis, self.acc, self.rej = [], [], 0, 0
        self.arm, self.ls_last = 90.0, [(None, 0.0)] * 3
        self.btn = {"marcha": 0, "paro": 0, "estop": 0}
        self.ins, self.ent, self.ls_now, self.shown = (1 << 1 | 1 << 4, 0), 0, [0, 0, 0], None
        self._build(); self._static()
        self.last_t = time.perf_counter()
        self.after(30, self.tick)

    # ---------- interfaz ----------
    def _slider(self, parent, name, lo, hi, init, unit):
        var = ctk.DoubleVar(value=init)
        lab = ctk.CTkLabel(parent, text=f"{name} = {init:.0f}{unit}", anchor="w"); lab.pack(fill="x", padx=14)
        ctk.CTkSlider(parent, from_=lo, to=hi, variable=var,
                      command=lambda v: lab.configure(text=f"{name} = {v:.0f}{unit}")).pack(fill="x", padx=14, pady=(0, 6))
        return var

    def _build(self):
        self.cv = tk.Canvas(self, width=900, height=660, bg="#14171c", highlightthickness=0)
        self.cv.pack(side="left", padx=(6, 0), pady=5)
        side = ctk.CTkFrame(self, width=320); side.pack(side="right", fill="y", padx=8, pady=8); side.pack_propagate(False)
        bold = ("Segoe UI", 13, "bold")
        ctk.CTkLabel(side, text="BottleSIM", font=("Segoe UI", 26, "bold")).pack(anchor="w", padx=14, pady=(8, 0))
        ctk.CTkLabel(side, text="Diseñado por ℳβ\nImplementado por: Claude AI", justify="left",
                     text_color="#8b949e").pack(anchor="w", padx=14)
        self.btn_feed = ctk.CTkButton(side, text="Producir Lote", command=self._toggle_feed)
        self.btn_feed.pack(fill="x", padx=14, pady=8)
        ctk.CTkLabel(side, text="Conteo", font=bold).pack(anchor="w", padx=14)
        self.lbl_acc = ctk.CTkLabel(side, text="Botellas aceptadas = 0", anchor="w"); self.lbl_acc.pack(fill="x", padx=14)
        self.lbl_rej = ctk.CTkLabel(side, text="Botellas rechazadas = 0", anchor="w"); self.lbl_rej.pack(fill="x", padx=14)
        ctk.CTkLabel(side, text="Controles", font=bold).pack(anchor="w", padx=14, pady=(6, 0))
        row = ctk.CTkFrame(side, fg_color="transparent"); row.pack(fill="x", padx=14)
        b = ctk.CTkButton(row, text="Marcha", width=84, fg_color="#2e7d32", hover_color="#388e3c"); b.pack(side="left")
        b.bind("<ButtonPress-1>", lambda e: self.btn.update(marcha=1)); b.bind("<ButtonRelease-1>", lambda e: self.btn.update(marcha=0))
        b = ctk.CTkButton(row, text="Paro", width=84, fg_color="#b26a00", hover_color="#c77800"); b.pack(side="left", padx=6)
        b.bind("<ButtonPress-1>", lambda e: self.btn.update(paro=1)); b.bind("<ButtonRelease-1>", lambda e: self.btn.update(paro=0))
        self.btn_es = ctk.CTkButton(row, text="E-Stop", width=84, fg_color="#c62828", hover_color="#d32f2f", command=self._toggle_es)
        self.btn_es.pack(side="left")
        ctk.CTkButton(side, text="Reiniciar conteos", fg_color="#444c56", command=self._reset).pack(fill="x", padx=14, pady=8)
        ctk.CTkLabel(side, text="Configuración", font=bold).pack(anchor="w", padx=14)
        self.speed = self._slider(side, "Velocidad de banda", 10, 150, 100, "%")
        self.fps = self._slider(side, "Frames por segundo", 10, 60, 30, "")
        self.defects = self._slider(side, "Tasa de defectos", 0, 100, 15, "%")
        self.io = tk.Canvas(side, width=290, height=176, bg="#1c2128", highlightthickness=0)
        self.io.pack(padx=14, pady=4); self.io.bind("<Button-1>", self._io_click)
        row2 = ctk.CTkFrame(side, fg_color="transparent"); row2.pack(fill="x", padx=14, pady=(2, 6))
        ctk.CTkButton(row2, text="Conexión PLC…", width=110, fg_color="#444c56", command=self.open_conn).pack(side="left")
        self.lbl_status = ctk.CTkLabel(row2, text="● Modo demo", text_color="#8b949e", anchor="w")
        self.lbl_status.pack(side="left", padx=8)

    def _toggle_feed(self):
        self.feeding = not self.feeding
        self.btn_feed.configure(text="Detener alimentación" if self.feeding else "Producir Lote",
                                fg_color="#b26a00" if self.feeding else ["#3a7ebf", "#1f538d"])

    def _toggle_es(self):
        self.btn["estop"] ^= 1
        self.btn_es.configure(text="E-Stop ACTIVO" if self.btn["estop"] else "E-Stop", fg_color="#7f0000" if self.btn["estop"] else "#c62828")

    def _reset(self):
        self.acc = self.rej = 0; self.rej_vis = []

    def open_conn(self):
        if self.conn_win is not None and not self.conn_win.closed:
            self.conn_win.lift(); return
        self.conn_win = ConnWindow(self)

    def _io_click(self, e):
        if self.link.demo and e.x >= 150:
            i = int((e.y - 26) // 18)
            if 0 <= i < 8:
                self.link.q ^= 1 << i

    # ---------- dibujo estático ----------
    def _txt(self, x, y, t, color="#c9d1d9", size=10, tags="static"):
        self.cv.create_text(x, y, text=t, fill=color, font=("Segoe UI", size, "bold"), tags=tags)

    def _static(self):
        c = self.cv
        pts = [v for p in (SEG[0][0], SEG[0][1], SEG[1][1], SEG[2][1]) for v in p]
        c.create_line(*pts, width=BELT_W + 8, fill="#262b33", joinstyle="round", capstyle="round")
        c.create_line(*pts, width=BELT_W, fill="#3a404a", joinstyle="round", capstyle="round")
        self._txt(150, 36, "ENTRADA", size=11); self._txt(750, 36, "SALIDA", size=11)
        self._txt(450, 360, "BANDA EN U", "#30363d", 16)
        for i, k in enumerate(K_FILL):                              # Llenadora
            y = pos(k * STEP)[1]
            c.create_rectangle(96, y - 11, 124, y + 11, fill="#5b6573", outline="#8b949e"); self._txt(110, y, f"E-{i+1}", size=8)
        self._txt(90, pos(K_FILL[0] * STEP)[1] - 30, "Llenadora", size=11)
        x, y, _ = pos(K_CAP * STEP)                                 # Taponadora (solo visual), después del nivel
        c.create_rectangle(x - 120, y - 18, x - 44, y + 18, fill="#2d333b", outline="#8b949e")
        c.create_line(x - 44, y, x - 30, y, fill="#8b949e", width=3); self._txt(x - 82, y, "Taponadora", size=9)
        for k in K_LS:                                              # sensores de nivel
            x = pos(k * STEP)[0]
            c.create_rectangle(x - 9, 420, x + 9, 436, fill="#2d333b", outline="#8b949e")
        self._txt(518, 555, "Medición de nivel", size=11)
        y = pos(K_REJ * STEP)[1]                                    # Rechazador y zona
        self._txt(650, y - 36, "Rechazador", size=11)
        c.create_rectangle(800, 230, 890, 370, outline="#8b949e", dash=(4, 3)); self._txt(845, 215, "Zona de Rechazados", size=9)

    # ---------- simulación ----------
    def step(self, dt, q, now):
        motor = q & 1
        move = V_BASE * self.speed.get() / 100 * dt if motor else 0.0
        self.travel += move
        for b in self.bottles[:]:
            if b.push:
                b.xoff += 260 * dt
                if b.xoff >= 95:
                    self.bottles.remove(b); self.rej += 1; self.rej_vis = (self.rej_vis + [b.fill])[-8:]
                continue
            b.s += move
            if not b.cap and b.s >= K_CAP * STEP:
                b.cap = True
            for i, k in enumerate(K_FILL):
                if (q >> (1 + i)) & 1 and abs(b.s - k * STEP) <= 0.25 * STEP:
                    b.fill = min(100.0, b.fill + 100 / T_LLENADO * b.k * dt)
            if b.s >= L:
                self.bottles.remove(b); self.acc += 1
            elif self.arm < 60 and -0.35 * STEP <= b.s - K_REJ * STEP <= 0.5 * STEP:
                b.push = True
        idx = int(self.travel // STEP)                              # alimentación: una botella por paso de banda
        if idx > self.last_idx:
            self.last_idx = idx
            if self.feeding:
                self.bottles.append(Bottle(self.travel % STEP, random.random() * 100 < self.defects.get()))
        target = 0 if (q >> 4) & 1 else 90                          # brazo rechazador
        self.arm += max(-360 * dt, min(360 * dt, target - self.arm))
        live = [b for b in self.bottles if not b.push]              # sensores
        self.ent = int(any(0 <= b.s < 0.35 * STEP for b in live))
        pulse = int(self.travel > 0 and self.travel % STEP < 0.35 * STEP)
        self.ls_now = [0, 0, 0]
        for i, k in enumerate(K_LS):
            for b in live:
                if -0.1 * STEP <= b.s - k * STEP < 0.35 * STEP:
                    self.ls_now[i] = int(b.fill >= NIVEL_OK); self.ls_last[i] = (b.fill, now)
        b8 = self.btn["marcha"] | ((0 if self.btn["paro"] else 1) << 1) | ((0 if self.btn["estop"] else 1) << 4)
        b9 = self.ent | pulse << 1 | self.ls_now[0] << 5 | self.ls_now[1] << 6 | self.ls_now[2] << 7
        self.ins = (b8, b9); self.link.inputs = bytearray(self.ins)

    # ---------- dibujo dinámico ----------
    def _led(self, x, y, on):
        self.cv.create_rectangle(x, y, x + 14, y + 14, fill="#37d67a" if on else "#0f3d24", outline="#0a2517", tags="dyn")

    def draw(self, q, now):
        c = self.cv; c.delete("dyn")
        off = self.travel % 34                                      # chevrones que avanzan con la banda
        for j in range(-1, L // 34 + 2):
            s = j * 34 + off
            if s < 14 or s > L - 14 or min(abs(s - 380), abs(s - 980)) < 20:
                continue
            x, y, (dx, dy) = pos(s); bx, by = x - dx * 6, y - dy * 6
            c.create_line(bx - dy * 11, by + dx * 11, x + dx * 6, y + dy * 6, bx + dy * 11, by - dx * 11,
                          fill="#6b7686", width=2, tags="dyn")
        c.create_line(100, 90, 178, 90, dash=(3, 3), fill="#ff5a5a" if self.ent else "#4a3030", tags="dyn")   # sensor de entrada
        c.create_oval(94, 84, 106, 96, fill="#ff5a5a" if self.ent else "#4a3030", outline="", tags="dyn")
        for i, k in enumerate(K_FILL):                              # válvulas
            y = pos(k * STEP)[1]; on = (q >> (1 + i)) & 1
            self._led(70, y - 7, on)
            if on:
                c.create_line(124, y, 136, y, fill="#3fa7ff", width=5, tags="dyn")
        self._led(100, 505, q & 1); self._txt(135, 512, "Banda", size=8, tags="dyn")
        for i, k in enumerate(K_LS):                                # medición de nivel
            x = pos(k * STEP)[0]
            c.create_line(x, 436, x, 442, fill="#ff5a5a" if self.ls_now[i] else "#5a3a3a", width=2, tags="dyn")
            f, t = self.ls_last[i]
            ok = f is not None and now - t < 1.5
            txt, col = (f"{f:.0f}%", "#37d67a" if f >= NIVEL_OK else "#ff6b6b") if ok else ("N/D", "#6e7681")
            c.create_rectangle(x - 30, 515, x + 30, 540, fill="#0d1117", outline="#30363d", tags="dyn")
            self._txt(x, 527, txt, col, 11, "dyn")
        for b in self.bottles:                                      # botellas
            x, y, _ = pos(b.s); x += b.xoff
            c.create_oval(x - 15, y - 15, x + 15, y + 15, fill="#1b2027", outline="#cfd8e3", width=2, tags="dyn")
            if b.fill > 0:
                r = 12 * math.sqrt(b.fill / 100)
                c.create_oval(x - r, y - r, x + r, y + r, fill="#3fa7ff", outline="", tags="dyn")
            if b.cap:
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#e5534b", outline="", tags="dyn")
        for i, f in enumerate(self.rej_vis):                        # zona de rechazados
            x, y = 815 + (i % 4) * 24, 262 + (i // 4) * 30
            c.create_oval(x - 9, y - 9, x + 9, y + 9, fill="#1b2027", outline="#cfd8e3", tags="dyn")
            r = 7 * math.sqrt(f / 100)
            c.create_oval(x - r, y - r, x + r, y + r, fill="#3fa7ff", outline="", tags="dyn")
        py = pos(K_REJ * STEP)[1]                                   # brazo rotatorio
        a = math.radians(self.arm)
        c.create_line(722, py, 722 + 56 * math.cos(a), py + 56 * math.sin(a), width=8, fill="#d08a2e", capstyle="round", tags="dyn")
        c.create_oval(716, py - 6, 728, py + 6, fill="#8b949e", outline="", tags="dyn")
        self._led(690, py + 10, (q >> 4) & 1)
        # panel de E/S
        io, F = self.io, ("Consolas", 9)
        io.delete("all")
        io.create_text(6, 10, anchor="w", text="Entradas", fill="#8b949e", font=F)
        io.create_text(150, 10, anchor="w", text="Salidas" + (" (clic=forzar)" if self.link.demo else ""), fill="#8b949e", font=F)
        for i, (n, by, bit) in enumerate(IN):
            io.create_rectangle(6, 26 + 18 * i, 18, 38 + 18 * i, fill="#37d67a" if (self.ins[by] >> bit) & 1 else "#0f3d24", outline="")
            io.create_text(24, 32 + 18 * i, anchor="w", text=n, fill="#c9d1d9", font=F)
        for i, n in enumerate(OUT):
            io.create_rectangle(150, 26 + 18 * i, 162, 38 + 18 * i, fill="#37d67a" if (q >> i) & 1 else "#0f3d24", outline="")
            io.create_text(168, 32 + 18 * i, anchor="w", text=n, fill="#c9d1d9", font=F)
        state = (self.acc, self.rej, self.link.connected, self.link.enabled)
        if state != self.shown:
            self.shown = state
            self.lbl_acc.configure(text=f"Botellas aceptadas = {self.acc}")
            self.lbl_rej.configure(text=f"Botellas rechazadas = {self.rej}")
            if self.link.connected:
                self.lbl_status.configure(text="● PLC conectado", text_color="#37d67a")
            elif self.link.enabled:
                self.lbl_status.configure(text="● Reconectando…", text_color="#e3b341")
            else:
                self.lbl_status.configure(text="● Modo demo", text_color="#8b949e")

    def tick(self):
        now = time.perf_counter(); dt = min(now - self.last_t, 0.1); self.last_t = now
        q = self.link.q
        self.step(dt, q, now); self.draw(q, now)
        self.after(int(1000 / self.fps.get()), self.tick)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip"); ap.add_argument("--rack", type=int, default=0); ap.add_argument("--slot", type=int, default=2)
    a = ap.parse_args()
    link = PLCLink(); link.rack, link.slot = a.rack, a.slot
    link.start()
    if a.ip:
        link.connect(a.ip, a.rack, a.slot)
    App(link).mainloop()
