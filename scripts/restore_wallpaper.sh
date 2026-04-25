#!/usr/bin/env bash
# =============================================================================
# restore_wallpaper.sh
# Propósito: Restaurar el fondo de pantalla al iniciar Hyprland.
# Problema que resuelve: hyprpaper v0.8.3 en Ubuntu 24 no puede pintar
# directamente (incompatibilidad de protocolo). swww pinta la imagen
# una vez y muere, dejando a hyprpaper como backend permanente (0% CPU).
# =============================================================================

# Leer la ruta del wallpaper guardada por el script Python
IMG=$(grep "^preload" ~/.config/hypr/hyprpaper.conf | cut -d'=' -f2 | tr -d ' ')

# Si no hay imagen guardada o no existe, no hacer nada
[ -z "$IMG" ] && exit 0
[ ! -f "$IMG" ] && exit 0

# Arrancar swww-daemon
swww-daemon &

# Esperar que esté listo
for i in $(seq 1 20); do
    sleep 2
    swww query > /dev/null 2>&1 && break
done

# Pintar la imagen sin transición
swww img "$IMG" --transition-type none

# Esperar que termine de pintar
sleep 1

# Informar a hyprpaper ANTES de matar swww
hyprctl hyprpaper preload "$IMG"
hyprctl hyprpaper wallpaper ",$IMG"
sleep 0.5

# swww muere — hyprpaper queda expuesto con la imagen ya pintada
killall -s SIGTERM swww-daemon
