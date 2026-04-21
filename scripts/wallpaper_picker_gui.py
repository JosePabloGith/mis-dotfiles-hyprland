#!/usr/bin/env python3
# =============================================================================
# wallpaper_picker_gui.py
# Autor: Pablo
# Última revisión: v6 (Cache Fix Edition)
#
# PROPÓSITO:
#   Selector visual de fondos de pantalla para Hyprland.
#   Barra inferior con miniaturas navegables por teclado.
#   Diseñado para 0% CPU en reposo — usa hyprpaper como backend permanente
#   y swww SOLO durante la transición, luego lo destruye.
#
# ARQUITECTURA "SÁNDWICH" + MATUGEN:
#   1. swww-daemon arranca y copia el fondo actual.
#   2. swww anima la transición al nuevo fondo (capa superior, 60fps, 1.5s).
#   3. EN PARALELO durante esos 1.5s: Matugen procesa el thumbnail del fondo
#      seleccionado y genera una paleta de colores. Hyprctl recarga Hyprland
#      al instante para colorear los bordes dinámicamente con el color
#      más dominante del nuevo fondo. Esto ocurre DENTRO del sándwich,
#      aprovechando el tiempo muerto de la animación swww.
#   4. hyprpaper carga el nuevo fondo por debajo en silencio.
#   5. swww-daemon muere → hyprpaper queda expuesto con la imagen ya puesta.
#
# CUÁNDO SE CAMBIAN LOS BORDES DE COLOR:
#   → Exactamente cuando el usuario presiona Enter y se lanza la animación swww.
#   → Matugen usa el THUMBNAIL en caché (160px) en lugar de la imagen completa,
#     lo que lo hace extremadamente rápido (~200ms) y sin consumir CPU extra.
#   → El borde cambia de color aproximadamente a los 300-500ms de la animación,
#     antes de que swww termine, dando una sensación de transición cohesiva.
#
# HISTORIAL DE FIXES:
#   v1-v3: Fixes visuales, hilos zombis y polling (ver repositorio).
#   v4:    Fallback robusto y parsing de listactive.
#   v5:    Integración nativa con Matugen usando thumbnails en caché.
#   v6:    Fix crítico de caché: hash basado en inode+tamaño+mtime en lugar
#          de la ruta de texto. Escritura atómica de PNG con os.replace().
#          Limpieza automática de thumbnails huérfanos y temporales zombis
#          en hilo daemon.
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
# CACHÉ DE MINIATURAS (v6: hash por metadatos del archivo, no por ruta)
# =============================================================================
# Por qué se cambió:
#   La versión anterior hasheaba la CADENA DE TEXTO de la ruta. Esto causaba
#   que al mover/renombrar un wallpaper se generara un nuevo thumbnail aunque
#   la imagen fuera idéntica. Con inode+tamaño+mtime_ns:
#     - El mismo archivo en distintas rutas produce el MISMO hash (via inode).
#     - Si el contenido cambia (mtime diferente), el hash cambia y se regenera.
#     - Es una operación de microsegundos: os.stat() no lee el archivo.
#
def thumb_cache_key(path):
    """Genera una llave de caché basada en metadatos del archivo, no en su ruta."""
    try:
        st = os.stat(path)
        # inode: identidad física del archivo en disco (estable ante renombrados)
        # st_size: si el tamaño cambia, el contenido cambió
        # st_mtime_ns: nanosegundos de modificación, más preciso que mtime float
        key = f"{st.st_ino}-{st.st_size}-{st.st_mtime_ns}"
    except OSError:
        # Fallback seguro: si no se puede stat (archivo inexistente en este momento),
        # se usa la ruta. Es transitorio y se autocorrige en la siguiente ejecución.
        key = path
    return key

def thumb_cache_path(path, size):
    h = hashlib.sha1(thumb_cache_key(path).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}_{size}.png")

def load_thumb_worker(src_path, image_widget, size, cancelled_flag):
    """
    Carga o genera el thumbnail de src_path y lo asigna al widget GTK.

    Escritura atómica:
      Se escribe primero en un archivo .tmp.PID y luego se hace os.replace()
      que en Linux es atómico dentro del mismo filesystem. Así, si dos hilos
      generan el mismo thumbnail simultáneamente, ninguno verá un PNG corrupto
      a mitad de escritura.
    """
    cache_path = thumb_cache_path(src_path, size)
    pix = None
    try:
        if os.path.exists(cache_path):
            # El thumbnail existe: cargarlo directamente sin abrir el original.
            # No es necesario re-validar mtime aquí porque el nombre del archivo
            # ya INCLUYE el mtime en su hash (ver thumb_cache_key). Si el
            # wallpaper cambia de contenido, su hash será diferente y este
            # archivo simplemente no existirá, forzando regeneración.
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(cache_path, size, size, True)

        if pix is None:
            # Thumbnail no existe o falló al cargar: generar desde el original.
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
            tmp_path = cache_path + f".tmp.{os.getpid()}"
            try:
                pix.savev(tmp_path, "png", [], [])
                # os.replace() es atómico en Linux (misma partición).
                # Garantiza que otros hilos/procesos nunca lean un PNG incompleto.
                os.replace(tmp_path, cache_path)
            except Exception as e:
                print(f"[wallpaper_picker] No se pudo guardar thumbnail: {e}")
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    except Exception as e:
        print(f"[wallpaper_picker] Error procesando {src_path}: {e}")
        pix = None

    if pix is not None and not cancelled_flag[0]:
        GLib.idle_add(image_widget.set_from_pixbuf, pix)

# =============================================================================
# LIMPIEZA DE CACHÉ HUÉRFANA Y ZOMBIS (v6 Final)
# =============================================================================
def limpiar_cache_huerfana(archivos_validos):
    """
    Elimina thumbnails de wallpapers que ya no existen en WALLPAPER_DIR.

    Se llama desde un hilo daemon al inicio, así no bloquea la UI.
    Calcula los hashes válidos actuales y borra todo lo demás del CACHE_DIR.

    Por qué es necesario:
      Sin esto, cada wallpaper eliminado o renombrado deja su PNG huérfano
      en caché para siempre. En colecciones grandes esto puede ocupar cientos
      de MB sin que nadie lo note. Además limpia archivos .tmp zombis.
    """
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
            # Manejar archivos temporales en progreso
            if ".tmp." in archivo:
                ruta_tmp = os.path.join(CACHE_DIR, archivo)
                try:
                    # Si el archivo temporal tiene más de 5 minutos (300 segundos), es un zombi. Lo borramos.
                    if time.time() - os.path.getmtime(ruta_tmp) > 300:
                        os.unlink(ruta_tmp)
                        print(f"[wallpaper_picker] Archivo temporal zombi eliminado: {archivo}")
                except Exception:
                    pass
                continue

            # Eliminar thumbnails huérfanos reales
            if archivo not in hashes_validos:
                try:
                    os.unlink(os.path.join(CACHE_DIR, archivo))
                    print(f"[wallpaper_picker] Caché huérfana eliminada: {archivo}")
                except Exception as e:
                    print(f"[wallpaper_picker] No se pudo borrar {archivo}: {e}")
    except Exception as e:
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
# CUÁNDO SE DISPARA MATUGEN Y POR QUÉ:
#
#   El usuario presiona Enter → la ventana se cierra → este hilo inicia.
#
#   Línea de tiempo:
#     t=0.00s  swww-daemon arranca (si no estaba corriendo)
#     t=0.00s  swww IMG empieza la animación de 1.5s en SEGUNDO PLANO (Popen)
#     t=0.00s  → Matugen arranca INMEDIATAMENTE en paralelo con swww
#     t≈0.20s  → Matugen termina de procesar el thumbnail (160px es muy liviano)
#     t≈0.20s  → hyprctl reload aplica la nueva paleta de colores a los bordes
#     t=1.70s  → swww termina la animación
#     t=1.70s  → hyprpaper toma el control definitivo (preload + wallpaper)
#     t=1.70s  → swww-daemon muere (killall SIGTERM)
#
#   Por qué usar el THUMBNAIL (160px) y no el wallpaper original:
#     - Matugen puede tardar 2-5s con una imagen de 4K.
#     - Con el thumbnail de 160px tarda ~200ms.
#     - El color dominante extraído es prácticamente idéntico en ambos casos.
#     - El thumbnail ya existe en caché desde que el usuario abrió el picker.
#
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

    # --- PASO 1: Animación visual (lanzada en segundo plano, no bloqueante) ---
    subprocess.Popen([
        "swww", "img", ruta_nueva,
        "--transition-type",     "center",
        "--transition-duration", "1.5",
        "--transition-fps",      "60",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- PASO 2: Matugen en paralelo con la animación swww ---
    # Mientras swww anima los 1.5s, Matugen procesa el thumbnail (ya en caché)
    # y recarga los colores de Hyprland. El usuario ve el borde cambiar de color
    # aproximadamente a mitad de la transición visual, creando un efecto cohesivo.
    if ruta_thumbnail and os.path.exists(ruta_thumbnail):
        subprocess.run([
            "matugen", "image", ruta_thumbnail,
            "--source-color-index", "0",  # índice 0 = color más dominante
            "-q"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.run([
            "hyprctl", "reload"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"[wallpaper_picker] Advertencia: thumbnail no encontrado para Matugen: {ruta_thumbnail}")
        print(f"[wallpaper_picker] Se omite el cambio de color de bordes para este fondo.")

    # --- PASO 3: Esperar a que swww termine su animación ---
    time.sleep(1.7)

    # --- PASO 4: Transferir control definitivo a hyprpaper (bajo consumo) ---
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

    # --- PASO 5: Destruir swww-daemon, hyprpaper queda expuesto ---
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

        # Limpieza de thumbnails huérfanos en hilo daemon (no bloquea la UI)
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

        # thumb_cache_path usa la misma llave (inode+size+mtime) que load_thumb_worker,
        # así se garantiza que apuntamos exactamente al thumbnail que ya existe en disco.
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
