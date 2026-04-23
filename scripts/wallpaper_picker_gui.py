#!/usr/bin/env python3
# =============================================================================
# wallpaper_picker_gui.py
# Autor: Pablo (Asistido por HyprGem)
# Última revisión: v15 (Enterprise Grade Edition)
#
# PROPÓSITO:
#   Selector visual de fondos de pantalla para Hyprland.
#   Barra inferior con miniaturas navegables por teclado.
#   Diseñado para 0% CPU en reposo — usa hyprpaper como backend permanente
#   y swww SOLO durante la transición, luego lo destruye.
#
# ARQUITECTURA "SÁNDWICH" + MATUGEN + THREAD-SAFETY:
#   1. swww-daemon arranca y copia el fondo actual.
#   2. swww anima la transición al nuevo fondo (capa superior, 60fps, 1.5s).
#   3. EN PARALELO durante esos 1.5s: Matugen procesa el thumbnail en caché
#      (160px, ~200ms) y hyprctl recarga los bordes dinámicamente.
#      El borde cambia de color aprox. a los 300-500ms de la animación,
#      dando una sensación de transición fluida y cohesiva.
#   4. hyprpaper carga el nuevo fondo por debajo en silencio.
#   5. swww-daemon muere (solo si fue iniciado por el script) → hyprpaper queda expuesto.
#
# HISTORIAL DE FIXES Y AUDITORÍAS:
#   v1-v5:   Fixes visuales, fallback robusto e integración nativa con Matugen.
#   v6:      Caché inteligente (inode+tamaño+mtime) y limpieza de archivos huérfanos.
#   v7-v8:   Protección RAM/CPU (Locks por archivo) y Anti-Zombis (Short-circuit).
#   v9-v10:  Protección SSD (sin rmtree) y respeto de demonios (daemon_ya_corria).
#   v11-v13: Consolidación de Arquitectura Sándwich (swww + matugen + hyprpaper).
#   v14-v15: Clean Code, prevención de deadlocks en D-Bus (Popen) y limpieza de hilos.
#   v16:     Generación Just-In-Time (JIT) de thumbnails para usuarios muy rápidos.
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

DEBUG_MODE     = False  # Cambia a True solo cuando estés desarrollando/probando

os.makedirs(CACHE_DIR, exist_ok=True)

# --- SISTEMA DE NOTIFICACIONES LIGERAS ---
def notificar_error(mensaje):
    """Muestra un error en pantalla (Wayland nativo) sin usar el disco duro para logs."""
    if DEBUG_MODE:
        try:
            # Fix: Usar Popen (fire-and-forget) evita que un D-Bus lento bloquee los locks de memoria
            subprocess.Popen(
                ["notify-send", "-u", "critical", "Wallpaper Picker", mensaje],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

# --- SISTEMA DE BLOQUEO DE HILOS ---
_thumb_locks = {}
_locks_mutex = threading.Lock()

def get_lock_for(key):
    with _locks_mutex:
        if key not in _thumb_locks:
            _thumb_locks[key] = threading.Lock()
        return _thumb_locks[key]

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
        if DEBUG_MODE:
            print(f"[wallpaper_picker] Error escribiendo config: {e}")

# =============================================================================
# CACHÉ DE MINIATURAS
# =============================================================================
def thumb_cache_key(path):
    try:
        st = os.stat(path)
        key = f"{st.st_ino}-{st.st_size}-{st.st_mtime_ns}"
    except OSError:
        key = path
    return key

def thumb_cache_path(path, size):
    h = hashlib.sha1(thumb_cache_key(path).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}_{size}.png")

def load_thumb_worker(src_path, image_widget, size, cancelled_flag):
    """Carga o genera el thumbnail protegiendo RAM y CPU."""
    # Short-circuit: Si se canceló la UI, muere inmediatamente sin consumir CPU.
    if cancelled_flag[0]:
        return

    cache_key = thumb_cache_key(src_path)
    cache_path = thumb_cache_path(src_path, size)
    lock = get_lock_for(cache_key)
    pix = None

    with lock:
        if cancelled_flag[0]:
            return

        try:
            if os.path.exists(cache_path):
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(cache_path, size, size, True)

            if pix is None:
                if cancelled_flag[0]:
                    return
                
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
                # Fix: Eliminado threading.get_ident() por ser redundante (el lock garantiza exclusividad)
                tmp_path = cache_path + f".tmp.{os.getpid()}"
                
                try:
                    pix.savev(tmp_path, "png", [], [])
                    os.replace(tmp_path, cache_path)
                except Exception as e:
                    if DEBUG_MODE:
                        print(f"[wallpaper_picker] ERROR THREAD - Guardar thumbnail: {e}")
                    try:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                    except Exception:
                        pass

        except Exception as e:
            if DEBUG_MODE:
                print(f"[wallpaper_picker] ERROR PROCESANDO {src_path}: {e}")
                notificar_error(f"Imagen corrupta o ilegible:\n{os.path.basename(src_path)}")
            pix = None

    if pix is not None and not cancelled_flag[0]:
        GLib.idle_add(image_widget.set_from_pixbuf, pix)

# =============================================================================
# LIMPIEZA DE CACHÉ HUÉRFANA Y ZOMBIS
# =============================================================================
def limpiar_cache_huerfana(archivos_validos):
    hashes_validos = set()
    for nombre in archivos_validos:
        ruta = os.path.join(WALLPAPER_DIR, nombre)
        try:
            h = hashlib.sha1(thumb_cache_key(ruta).encode("utf-8")).hexdigest()
            hashes_validos.add(f"{h}_{THUMBNAIL_SIZE}.png")
        except Exception:
            pass

    try:
        for archivo in os.listdir(CACHE_DIR):
            if ".tmp." in archivo:
                ruta_tmp = os.path.join(CACHE_DIR, archivo)
                try:
                    if time.time() - os.path.getmtime(ruta_tmp) > 300:
                        os.unlink(ruta_tmp)
                except Exception:
                    pass
                continue

            if archivo not in hashes_validos:
                try:
                    os.unlink(os.path.join(CACHE_DIR, archivo))
                except Exception:
                    pass
    except Exception as e:
        if DEBUG_MODE:
            print(f"[wallpaper_picker] Error durante limpieza de caché: {e}")

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
# WORKER DEL SÁNDWICH + MATUGEN
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

        # Protección SSD: NO borrar el directorio masivamente con rmtree.
        # En su lugar, dejamos que el demonio maneje su propia memoria/caché.
        # Es mejor no tocar el disco duro innecesariamente.

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

    subprocess.Popen([
        "swww", "img", ruta_nueva,
        "--transition-type",     "center",
        "--transition-duration", "1.5",
        "--transition-fps",      "60",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Fallback JIT (v16): Si el usuario aplicó el fondo rápido antes de que el worker lo procesara
    if ruta_thumbnail and not os.path.exists(ruta_thumbnail):
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(ruta_nueva, THUMBNAIL_SIZE, THUMBNAIL_SIZE, True)
            tmp_path = ruta_thumbnail + f".tmp.{os.getpid()}"
            pix.savev(tmp_path, "png", [], [])
            os.replace(tmp_path, ruta_thumbnail)
        except Exception:
            pass

    if ruta_thumbnail and os.path.exists(ruta_thumbnail):
        r = subprocess.run([
            "matugen", "image", ruta_thumbnail,
            "--source-color-index", "0",
            "-q"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if r.returncode != 0 and DEBUG_MODE:
            notificar_error("Matugen falló al extraer colores del thumbnail.")

        subprocess.run([
            "hyprctl", "reload"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        if DEBUG_MODE:
            notificar_error("Thumbnail no encontrado. Se omitió la recarga de colores.")

    time.sleep(1.7)

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

    # Control de demonio: Respetamos el proceso si no lo abrimos nosotros.
    if not daemon_ya_corria:
        subprocess.run(
            ["killall", "-s", "SIGTERM", "swww-daemon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    elif DEBUG_MODE:
        print("[wallpaper_picker] swww-daemon ya corría previamente. No se cerrará.")

    actualizar_config(ruta_nueva)
    GLib.idle_add(Gtk.main_quit)

# =============================================================================
# VENTANA PRINCIPAL
# =============================================================================
class WallpaperBar(Gtk.Window):
    def __init__(self):
        super().__init__(title="wallpaper_picker")

        self._cancelled = [False]
        # El Executor es local a la instancia para evitar procesos zombis en RAM.
        self.executor = ThreadPoolExecutor(max_workers=THUMB_WORKERS)

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

        threading.Thread(
            target=limpiar_cache_huerfana,
            args=(archivos,),
            daemon=True
        ).start()

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

            self.executor.submit(
                load_thumb_worker, ruta, imagen, THUMBNAIL_SIZE, self._cancelled
            )

        self.actualizar_seleccion()
        self.connect("key-press-event", self.on_tecla)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_):
        self._cancelled[0] = True
        self.executor.shutdown(wait=False, cancel_futures=True)

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
