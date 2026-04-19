#!/bin/bash
# =============================================================================
# wallpaper_picker.sh
# Autor: Pablo
# Descripcion: Menu rofi para elegir fondo de pantalla en Hyprland.
#              - Arquitectura "Sándwich" (swww temporal + hyprpaper permanente).
#              - Sistema de caché de miniaturas para proteger el SSD y abrir rápido.
# =============================================================================

# --- CONFIGURACION ---
WALLPAPER_DIR="$HOME/Imagenes/wallpapers/hyperLand_wallpapers"
HYPRLAND_CONF="$HOME/.config/hypr/hyprpaper.conf"
CACHE_DIR="$HOME/.cache/wallpaper_picker/bash_thumbs"
THUMB_SIZE="160x160"

# Crear directorio de caché si no existe
mkdir -p "$CACHE_DIR"

# --- FUNCIONES CORE ---

update_config() {
    local img="$1"
    cat <<EOF > "$HYPRLAND_CONF"
splash = false
ipc = true
preload = $img
wallpaper = ,$img
EOF
}

get_current_wallpaper() {
    local current=""
    local listactive
    listactive=$(hyprctl hyprpaper listactive 2>/dev/null)
    
    local edp_path=$(echo "$listactive" | grep "eDP" | cut -d'=' -f2 | tr -d ' ')
    local fallback_path=$(echo "$listactive" | grep "=" | head -n 1 | cut -d'=' -f2 | tr -d ' ')
    
    if [ -n "$edp_path" ] && [ -f "$edp_path" ]; then
        current="$edp_path"
    elif [ -n "$fallback_path" ] && [ -f "$fallback_path" ]; then
        current="$fallback_path"
    elif [ -f "$HYPRLAND_CONF" ]; then
        local conf_path=$(grep "^wallpaper" "$HYPRLAND_CONF" | cut -d',' -f2 | tr -d ' ')
        if [ -n "$conf_path" ] && [ -f "$conf_path" ]; then
            current="$conf_path"
        fi
    fi
    echo "$current"
}

apply_sandwich() {
    local ruta_nueva="$1"
    local current_wall=$(get_current_wallpaper)

    if ! pgrep -x swww-daemon > /dev/null; then
        rm -rf ~/.cache/swww
        swww-daemon > /dev/null 2>&1 &
        local iters=0
        while [ $iters -lt 20 ]; do
            sleep 0.1
            if swww query > /dev/null 2>&1; then break; fi
            iters=$((iters + 1))
        done
    fi

    if [ -n "$current_wall" ] && [ -f "$current_wall" ]; then
        swww img "$current_wall" --transition-type none > /dev/null 2>&1
        sleep 0.15
    fi

    swww img "$ruta_nueva" \
        --transition-type center \
        --transition-duration 1.5 \
        --transition-fps 60 > /dev/null 2>&1
    
    sleep 1.7

    hyprctl hyprpaper preload "$ruta_nueva" > /dev/null 2>&1
    hyprctl hyprpaper wallpaper ",$ruta_nueva" > /dev/null 2>&1
    hyprctl hyprpaper unload unused > /dev/null 2>&1

    killall -s SIGTERM swww-daemon > /dev/null 2>&1
    update_config "$ruta_nueva"
}

# --- SISTEMA DE CACHÉ Y PREPARACIÓN DEL MENÚ ---
MENU_ITEMS=""

for img in "$WALLPAPER_DIR"/*; do
    if [ -f "$img" ]; then
        filename=$(basename "$img")
        thumb_path="$CACHE_DIR/${filename}.png"

        # Si la miniatura no existe, o la imagen original es más nueva (-nt) que la miniatura
        if [ ! -f "$thumb_path" ] || [ "$img" -nt "$thumb_path" ]; then
            # Generar miniatura rápida usando ImageMagick
            # (-thumbnail es más rápido que -resize porque descarta metadatos)
            magick "$img" -thumbnail "${THUMB_SIZE}^" -gravity center -extent "$THUMB_SIZE" "$thumb_path" 2>/dev/null || \
            convert "$img" -thumbnail "${THUMB_SIZE}^" -gravity center -extent "$THUMB_SIZE" "$thumb_path" 2>/dev/null
        fi

        # Agregar a la lista apuntando a la miniatura en caché, NO a la imagen original
        MENU_ITEMS+="${filename}\0icon\x1f${thumb_path}\n"
    fi
done

# --- MENU ROFI ---
# 'echo -en' procesa los saltos de linea y separadores de rofi
SELECTED=$(echo -en "$MENU_ITEMS" | rofi \
    -dmenu \
    -show-icons \
    -p "Fondo de pantalla" \
    -theme-str '
    window {
        background-color: rgba(20, 20, 30, 0.92);
        border: 2px;
        border-color: rgba(0, 255, 153, 0.9);
        border-radius: 12px;
        width: 550px;
    }
    mainbox { background-color: transparent; padding: 12px; }
    inputbar {
        background-color: rgba(0, 255, 153, 0.08);
        border: 1px; border-color: rgba(51, 204, 255, 0.5);
        border-radius: 8px; padding: 8px; text-color: #33ccff;
    }
    prompt { text-color: #00ff99; }
    entry { text-color: #ffffff; }
    listview { background-color: transparent; padding: 6px 0px; lines: 6; }
    element {
        background-color: transparent;
        padding: 8px 12px;
        border-radius: 6px;
        text-color: #cccccc;
    }
    element-icon {
        size: 4em;
        cursor: inherit;
        background-color: transparent;
        text-color: inherit;
    }
    element-text {
        vertical-align: 0.5;
        background-color: transparent;
        text-color: inherit;
    }
    element selected {
        background-color: rgba(0, 255, 153, 0.15);
        text-color: #00ff99;
        border: 1px; border-color: rgba(51, 204, 255, 0.6);
        border-radius: 6px;
    }
    ')

# --- VALIDACION ---
[ -z "$SELECTED" ] && exit 0

FULL_PATH="$WALLPAPER_DIR/$SELECTED"

# Aplicar el fondo en segundo plano
apply_sandwich "$FULL_PATH" &
disown
