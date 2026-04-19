#!/usr/bin/env bash
# =============================================================================
# SCRIPT: random_lock.sh (Gestor de Bloqueo Dinámico y Seguro)
# AUTOR: Pablo
# =============================================================================
# PROPÓSITO:
#   Selecciona de forma aleatoria una imagen de mi galeria personal: 
#   (/home/pablo/Imagenes/wallpapers/hyperLand_wallpapers) y la configura como
#   fondo para 'hyprlock', garantizando que el equipo se bloquee instantáneamente
#   y de forma segura al ejecutarse.
#
# DEPENDENCIAS REQUERIDAS (Herramientas estándar de Linux):
#   - bash       (Intérprete del script)
#   - findutils  (Provee el comando 'find' para búsquedas eficientes)
#   - sed        (Editor de flujo para inyección de configuración)
#   - coreutils  (Provee 'mktemp' y operaciones de archivos)
#   - hyprlock   (El bloqueador de pantalla nativo del ecosistema Hyprland)
#
# ARQUITECTURA Y OPTIMIZACIÓN (Por qué está diseñado así):
#   1. Caché Ultra-Rápido (.index): Leer el disco duro para buscar imágenes cada 
#      vez que bloqueas la pantalla introduce latencia (retraso). Este script 
#      crea un archivo de texto con las rutas y solo vuelve a usar el disco si 
#      detecta que has descargado una imagen nueva.
#   2. Inyección Directa (Fix Wayland): Históricamente se usaban Enlaces Simbólicos
#      (symlinks) para cambiar fondos. Sin embargo, las cachés gráficas modernas de 
#      Wayland a veces ignoran los symlinks. Este script usa 'sed' para reescribir 
#      directamente la línea 'path =' dentro de tu hyprlock.conf, forzando la 
#      actualización del fondo con 100% de fiabilidad.
#   3. Fail-Safe (A prueba de fallos): La seguridad es crítica. Si las imágenes 
#      se borran o el caché se corrompe, el script atrapará el error y ejecutará 
#      el bloqueo de emergencia (exec hyprlock) de todas formas.
#   4. Exec (0% CPU residual): El comando final 'exec hyprlock' destruye este script
#      de la memoria RAM y lo reemplaza por el proceso de bloqueo, ahorrando recursos.
#
# INSTRUCCIONES DE MODIFICACIÓN:
#   - Si mueves tu carpeta de imágenes, actualiza la variable 'WALLPAPER_DIR'.
#   - Si agregas nuevos formatos de imagen (ej. .gif), agrégalos en las funciones
#     'build_index' y 'index_needs_refresh' copiando la estructura '-iname'.
# =============================================================================

# ── 1. CONFIGURACIÓN BASE Y SEGURIDAD ────────────────────────────────────
# -u: Falla si intentamos usar una variable no definida. 
# Nota: Se omite '-e' intencionalmente para que un fallo intermedio no impida el bloqueo.
set -u

# IFS (Internal Field Separator): Solo usamos saltos de línea y tabuladores 
# para proteger los nombres de archivo que contengan espacios.
IFS=$'\n\t'

# Rutas del entorno (MODIFICAR AQUÍ SI CAMBIAS TUS CARPETAS)
WALLPAPER_DIR="$HOME/Imagenes/wallpapers/hyperLand_wallpapers"
CACHE_DIR="$HOME/.cache/hyprlock"
INDEX_PATH="$CACHE_DIR/hyprlock_wallpapers.index"
HYPRLOCK_CONF="$HOME/.config/hypr/hyprlock.conf"

# Asegura que el directorio de la caché exista antes de operar
mkdir -p "$CACHE_DIR"

# ── 2. VALIDACIÓN CRÍTICA INICIAL ────────────────────────────────────────
# Si la carpeta principal de fondos no existe, lanza un aviso a los logs pero 
# EJECUTA EL BLOQUEO INMEDIATAMENTE. La seguridad del equipo es la prioridad.
if [[ ! -d "$WALLPAPER_DIR" ]]; then
    echo "No existe la carpeta de wallpapers: $WALLPAPER_DIR" >&2
    exec hyprlock
fi

# ── 3. FUNCIÓN: CONSTRUIR ÍNDICE (CACHÉ) ─────────────────────────────────
# Escanea la carpeta y guarda las rutas de las imágenes en un archivo de texto.
build_index() {
    local tmp_index
    # Se usa mktemp para evitar colisiones de archivos temporales en RAM
    tmp_index="$(mktemp "$CACHE_DIR/hyprlock_wallpapers.index.XXXXXX")"

    # Busca imágenes y usa caracteres nulos (-print0) por si tienen espacios o símbolos raros
    find "$WALLPAPER_DIR" -type f \( \
        -iname "*.jpg" -o \
        -iname "*.jpeg" -o \
        -iname "*.png" -o \
        -iname "*.webp" \
    \) -print0 > "$tmp_index"

    # Operación atómica: reemplaza el índice viejo por el nuevo sin corromperlo
    mv -f "$tmp_index" "$INDEX_PATH"
}

# ── 4. FUNCIÓN: COMPROBADOR DE CAMBIOS ───────────────────────────────────
# Determina de forma ultra-rápida si necesitamos actualizar el índice.
index_needs_refresh() {
    # 1. Si no hay índice o está vacío, requiere refresco (return 0)
    [[ ! -s "$INDEX_PATH" ]] && return 0

    # 2. Busca AL MENOS un archivo más nuevo que el archivo índice (-newer). 
    # [[ -n ]] devuelve verdadero si la búsqueda encuentra algo nuevo.
    [[ -n "$(find "$WALLPAPER_DIR" -type f \( \
        -iname "*.jpg" -o \
        -iname "*.jpeg" -o \
        -iname "*.png" -o \
        -iname "*.webp" \
    \) -newer "$INDEX_PATH" -print -quit 2>/dev/null)" ]]
}

# ── 5. FUNCIÓN: MOTOR DE ALEATORIEDAD ────────────────────────────────────
# Selecciona la imagen leyendo el caché y verificando que el archivo aún exista.
pick_random_wallpaper() {
    local wallpapers=()

    # Si hay imágenes nuevas descargadas, reconstruye el índice primero
    if index_needs_refresh; then
        build_index
    fi

    # Si el índice sigue vacío (carpeta sin imágenes), abortar silenciosamente
    [[ -s "$INDEX_PATH" ]] || return 1

    # Cargamos el índice en un array de bash, separando por el delimitador nulo
    mapfile -d '' -t wallpapers < "$INDEX_PATH" || true

    local valid_wallpapers=()
    local wp
    
    # PROTECCIÓN CONTRA FANTASMAS: Verificamos que cada archivo listado en el índice 
    # siga existiendo físicamente en el disco (por si borraste una imagen a mano).
    for wp in "${wallpapers[@]}"; do
        [[ -f "$wp" ]] && valid_wallpapers+=("$wp")
    done

    # Reasignamos el array solo con las imágenes comprobadas (válidas)
    wallpapers=("${valid_wallpapers[@]}")
    
    # Si después de filtrar no queda ninguna imagen, abortamos
    (( ${#wallpapers[@]} > 0 )) || return 1

    # Selecciona y devuelve matemáticamente una ruta al azar (RANDOM)
    printf '%s\n' "${wallpapers[RANDOM % ${#wallpapers[@]}]}"
}

# ── 6. EJECUCIÓN PRINCIPAL Y APLICACIÓN ──────────────────────────────────
# Si la selección falla, '|| true' atrapa el error para que el script no colapse
RANDOM_PIC="$(pick_random_wallpaper)" || true

# Si obtuvimos una imagen válida (la variable no está vacía), inyectamos la ruta
if [[ -n "${RANDOM_PIC:-}" ]]; then
    # INYECCIÓN NATIVA (SED): Buscamos la línea "path = ..." en el archivo de hyprlock
    # y la sustituimos directamente por la ruta absoluta de la nueva imagen.
    if [[ -f "$HYPRLOCK_CONF" ]]; then
        sed -i "s|^ *path = .*|    path = $RANDOM_PIC|" "$HYPRLOCK_CONF"
    fi
fi

# ── 7. LANZAMIENTO DEL BLOQUEO DE SEGURIDAD ──────────────────────────────
# Sustituye este script en memoria por el proceso hyprlock.
# Al estar al final y fuera de los "if", GARANTIZA que la pantalla siempre se bloquee.
exec hyprlock
