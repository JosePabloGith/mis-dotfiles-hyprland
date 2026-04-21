#!/usr/bin/env python3
# =============================================================================
# wallpaper_picker_gui.py
# Autor: Pablo
# Última revisión: v5 (Matugen Edition)
#
# PROPÓSITO:
#   Selector visual de fondos de pantalla para Hyprland.
#   Barra inferior con miniaturas navegables por teclado.
#   Diseñado para 0% CPU en reposo — usa hyprpaper como backend permanente
#   y swww SOLO durante la transición, luego lo destruye.
#
# ARQUITECTURA "SÁNDWICH" + MATUGEN:
#   1. swww-daemon arranca y copia el fondo actual.
#   2. swww anima la transición al nuevo fondo (capa superior, 60fps).
#   3. EN PARALELO: Matugen procesa la miniatura y genera una paleta de colores,
#      recargando Hyprland al instante para colorear los bordes dinámicamente.
#   4. hyprpaper carga el nuevo fondo por debajo en silencio.
#   5. swww-daemon muere → hyprpaper queda expuesto con la imagen ya puesta.
#
# HISTORIAL DE FIXES:
#   v1 a v3: Fixes visuales, hilos zombis y polling (ver repositorio).
#   v4: Fallback robusto y parsing de listactive.
#   v5: Integración nativa con Matugen usando hashes SHA-1 compartidos
#       para extraer colores desde la caché sin estrés de CPU.
# =============================================================================

import gi
import os
import subprocess
import hashlib
import time
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, GdkPixbuf, Gdk, GLib, Pango

# =============================================================================
# CONFIGURACIÓN
# =============================================================================
WALLPAPER_DIR  = os.path.expanduser("~/Imagenes/wallpapers/hyperLand_wallpapers")
THUMBNAIL_SIZE = 160
EXTENSIONES    = ('.jpg', '.jpeg', '.png', '.webp')
ALTURA_BARRA   = 220
CACHE_DIR      = os.path.expanduser("~/.cache/wallpaper_picker/thumbnails")
THUMB_WORKERS  = 4
HYPRPAPER_CONF = os.path.expanduser("~/.config/hypr/hyprpaper.conf")
SWWW_CACHE_DIR = os.path.expanduser("~/.cache/swww")

SWWW_TIMEOUT   = 2.0
SWWW_POLL_MS   = 0.1

os.makedirs(CACHE_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=THUMB_WORKERS)

# =============================================================================
# CSS
# =============================================================================
CSS = b"""
window {
    background-color: rgba(15, 15, 25, 0.92);
    border-top: 2px solid rgba(0, 255, 153, 0.9);
    border-radius: 16px;
}
scrolledwindow { background-color: transparent; }
viewport       { background-color: transparent; }

#contenedor {
    background-color: transparent;
    padding: 12px 16px;
}
#titulo {
    color: #00ff99;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 16px;
}
.thumb-box {
    background-color: transparent;
    border-radius: 8px;
    padding: 6px;
    border: 2px solid transparent;
}
.thumb-box:hover {
    border: 2px solid rgba(51, 204, 255, 0.5);
    background-color: rgba(51, 204, 255, 0.08);
}
.thumb-selected {
    border: 2px solid rgba(0, 255, 153, 0.9);
    background-color: rgba(0, 255, 153, 0.12);
    border-radius: 8px;
    padding: 6px;
}
.thumb-label {
    color: #888888;
    font-size: 10px;
    padding-top: 4px;
}
.thumb-label-selected {
    color: #00ff99;
    font-size: 10px;
    font-weight: bold;
    padding-top: 4px;
}
"""

# =============================================================================
# PERSISTENCIA
# =============================================================================
def actualizar_config(img_path):
    contenido = (
        "splash = false\n"
        "ipc = true\n"
        f"preload = {img_path}\n"
        f"wallpaper = ,{img_path}\n"
    )
    try:
        with open(HYPRPAPER_CONF, 'w') as f:
            f.write(contenido)
    except Exception as e:
        print(f"[wallpaper_picker] Error escribiendo config: {e}")

# =============================================================================
# CACHÉ DE MINIATURAS (HASH SHA-1)
# =============================================================================
def thumb_cache_path(path, size):
    # Usamos SHA-1 de forma unificada en todo el script para evitar conflictos
    h = hashlib.sha1(path.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}_{size}.png")

def load_thumb_worker(src_path, image_widget, size, cancelled_flag):
    cache_path = thumb_cache_path(src_path, size)
    pix = None
    try:
        if os.path.exists(cache_path):
            if os.path.getmtime(cache_path) >= os.path.getmtime(src_path):
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(cache_path, size, size, True)
        if pix is None:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
            try:
                pix.savev(cache_path, "png", [], [])
            except Exception:
                pass 
    except Exception:
        pix = None

    if pix is not None and not cancelled_flag[0]:
        GLib.idle_add(image_widget.set_from_pixbuf, pix)

# =============================================================================
# DETECCIÓN DEL FONDO ACTUAL
# =============================================================================
def obtener_fondo_actual():
    try:
        salida = subprocess.check_output(
            ["hyprctl", "hyprpaper", "listactive"],
            text=True, timeout=2
        )
        lineas = [l for l in salida.splitlines() if "=" in l]

        candidato_principal = None
        candidato_fallback  = None

        for linea in lineas:
            monitor, _, ruta = linea.partition("=")
            ruta = ruta.strip()
            if os.path.isfile(ruta):
                if candidato_fallback is None:
                    candidato_fallback = ruta
                if "eDP" in monitor:
                    candidato_principal = ruta
                    break

        resultado = candidato_principal or candidato_fallback
        if resultado:
            return resultado
    except Exception:
        pass

    try:
        with open(HYPRPAPER_CONF) as f:
            for linea in f:
                stripped = linea.strip()
                if stripped.startswith("wallpaper"):
                    if "," in stripped:
                        ruta = stripped.split(",", 1)[1].strip()
                    elif "=" in stripped:
                        ruta = stripped.split("=", 1)[1].strip()
                    else:
                        continue
                    if os.path.isfile(ruta):
                        return ruta
    except Exception:
        pass

    return None 

# =============================================================================
# WORKER DEL SÁNDWICH (AHORA CON MATUGEN INTEGRADO)
# =============================================================================
def _sandwich_worker(ruta_nueva, current_wall, ruta_thumbnail):
    daemon_ya_corria = True
    try:
        subprocess.run(
            ["pgrep", "-x", "swww-daemon"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        daemon_ya_corria = False
        
        if os.path.exists(SWWW_CACHE_DIR):
            try:
                shutil.rmtree(SWWW_CACHE_DIR)
            except Exception:
                pass

        subprocess.Popen(
            ["swww-daemon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        transcurrido = 0.0
        while transcurrido < SWWW_TIMEOUT:
            time.sleep(SWWW_POLL_MS)
            transcurrido += SWWW_POLL_MS
            r = subprocess.run(
                ["swww", "query"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if r.returncode == 0:
                break 

    if current_wall and os.path.isfile(current_wall):
        subprocess.run(
            ["swww", "img", current_wall, "--transition-type", "none"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(0.15) 

    # 1. Inicia la animación visual con swww (en segundo plano)
    subprocess.Popen([
        "swww", "img", ruta_nueva,
        "--transition-type",     "center",
        "--transition-duration", "1.5",
        "--transition-fps",      "60",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ==========================================================
    # 2. INYECCIÓN MATUGEN: Extracción y aplicación de colores
    # ==========================================================
    # Mientras transcurren los 1.5s de la animación swww, Matugen actúa
    if ruta_thumbnail and os.path.exists(ruta_thumbnail):
        # Ejecuta Matugen en modo silencioso tomando el color más dominante (index 0)
        subprocess.run([
            "matugen", "image", ruta_thumbnail, 
            "--source-color-index", "0", "-q"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Recarga Hyprland instantáneamente para pintar los bordes de la UI
        subprocess.run([
            "hyprctl", "reload"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"Advertencia: No se encontró el thumbnail para Matugen: {ruta_thumbnail}")
    # ==========================================================

    # 3. Espera a que termine la animación de swww
    time.sleep(1.7)

    # 4. Traspasa el control definitivo a Hyprpaper (Bajo consumo)
    subprocess.run(
        ["hyprctl", "hyprpaper", "preload", ruta_nueva],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    subprocess.run(
        ["hyprctl", "hyprpaper", "wallpaper", f",{ruta_nueva}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    subprocess.run(
        ["hyprctl", "hyprpaper", "unload", "unused"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    subprocess.run(
        ["killall", "-s", "SIGTERM", "swww-daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    actualizar_config(ruta_nueva)
    GLib.idle_add(Gtk.main_quit)

# =============================================================================
# VENTANA PRINCIPAL
# =============================================================================
class WallpaperBar(Gtk.Window):
    def __init__(self):
        super().__init__(title="wallpaper_picker")

        self._cancelled = [False]

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)

        display = Gdk.Display.get_default()
        monitor = display.get_monitor(0)
        if monitor:
            geo   = monitor.get_geometry()
            width = geo.width
            x_pos = geo.x
            y_pos = geo.y + geo.height - ALTURA_BARRA
        else:
            width, x_pos, y_pos = 1920, 0, 1080 - ALTURA_BARRA

        self.set_default_size(width, ALTURA_BARRA)
        self.move(x_pos, y_pos)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        titulo = Gtk.Label(
            label="  Elige tu fondo  |  ← → navegar  |  Enter aplicar  |  Esc cerrar"
        )
        titulo.set_name("titulo")
        titulo.set_halign(Gtk.Align.START)
        vbox.pack_start(titulo, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.scroll.set_min_content_height(ALTURA_BARRA - 40)
        vbox.pack_start(self.scroll, True, True, 0)

        contenedor = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        contenedor.set_name("contenedor")
        self.scroll.add(contenedor)

        self.items        = []
        self.seleccionado = 0

        archivos = []
        if os.path.exists(WALLPAPER_DIR):
            archivos = sorted([
                f for f in os.listdir(WALLPAPER_DIR)
                if f.lower().endswith(EXTENSIONES)
            ])

        for nombre in archivos:
            ruta = os.path.join(WALLPAPER_DIR, nombre)

            vbox_item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            vbox_item.get_style_context().add_class("thumb-box")
            vbox_item.ruta   = ruta
            vbox_item.nombre = nombre

            imagen = Gtk.Image()
            imagen.set_size_request(THUMBNAIL_SIZE, THUMBNAIL_SIZE)

            label = Gtk.Label(label=nombre)
            label.get_style_context().add_class("thumb-label")
            label.set_max_width_chars(18)
            label.set_ellipsize(Pango.EllipsizeMode.END)

            vbox_item.pack_start(imagen, False, False, 0)
            vbox_item.pack_start(label,  False, False, 0)
            vbox_item.imagen_widget = imagen
            vbox_item.label_widget  = label

            contenedor.pack_start(vbox_item, False, False, 0)
            self.items.append(vbox_item)

            executor.submit(
                load_thumb_worker, ruta, imagen, THUMBNAIL_SIZE, self._cancelled
            )

        self.actualizar_seleccion()
        self.connect("key-press-event", self.on_tecla)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_):
        self._cancelled[0] = True
        executor.shutdown(wait=False, cancel_futures=True)

    def actualizar_seleccion(self):
        for i, item in enumerate(self.items):
            ctx  = item.get_style_context()
            lctx = item.label_widget.get_style_context()
            if i == self.seleccionado:
                ctx.remove_class("thumb-box");      ctx.add_class("thumb-selected")
                lctx.remove_class("thumb-label");   lctx.add_class("thumb-label-selected")
            else:
                ctx.remove_class("thumb-selected"); ctx.add_class("thumb-box")
                lctx.remove_class("thumb-label-selected"); lctx.add_class("thumb-label")
        GLib.idle_add(self._scroll_to_selected)

    def _scroll_to_selected(self):
        if not self.items:
            return
        item  = self.items[self.seleccionado]
        alloc = item.get_allocation()
        adj   = self.scroll.get_hadjustment()
        adj.set_value(alloc.x - (adj.get_page_size() / 2) + (alloc.width / 2))

    def aplicar_fondo(self):
        if not self.items:
            return

        ruta_nueva   = self.items[self.seleccionado].ruta
        current_wall = obtener_fondo_actual()

        # Usamos tu propia función de caché (SHA-1) para garantizar que la ruta 
        # enviada a Matugen sea exactamente la misma que la del thumbnail generado
        ruta_thumbnail = thumb_cache_path(ruta_nueva, THUMBNAIL_SIZE)

        self.destroy()

        threading.Thread(
            target=_sandwich_worker,
            args=(ruta_nueva, current_wall, ruta_thumbnail),
            daemon=True
        ).start()

    def on_tecla(self, widget, event):
        k = event.keyval
        if k == Gdk.KEY_Escape:
            self.destroy()
            Gtk.main_quit()
        elif k == Gdk.KEY_Return:
            self.aplicar_fondo()
        elif k == Gdk.KEY_Right:
            self.seleccionado = (self.seleccionado + 1) % len(self.items)
            self.actualizar_seleccion()
        elif k == Gdk.KEY_Left:
            self.seleccionado = (self.seleccionado - 1) % len(self.items)
            self.actualizar_seleccion()

if __name__ == "__main__":
    win = WallpaperBar()
    win.show_all()
    Gtk.main()
