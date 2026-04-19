#!/usr/bin/env bash
# =======================================================================================
# MÓDULO: HERRAMIENTA DE CAPTURAS DE PANTALLA (screenshot-named.sh)
# AUTOR: Pablo
# PLATAFORMA: Hyprland 0.54.3+ (Wayland)
# =======================================================================================
# PROPÓSITO:
#   Proveer un sistema de capturas seguro, atómico y modular. Congela el framebuffer
#   para recortes precisos y gestiona los recursos con estándares de alta seguridad.
#
# AUDITORÍA DE SEGURIDAD (Zero-Fail / DevSecOps):
#   - Path Traversal Protection: Sanitiza los nombres de archivo ingresados en la GUI.
#   - Atomic Cross-Device Save: Garantiza integridad al copiar de RAM (tmpfs) a SSD.
#   - IPC Polling Sync & Fail-Close: Elimina condiciones de carrera y aborta de forma 
#     segura si el congelador (hyprpicker) no logra inicializarse a tiempo.
#   - Anti-Overwrite: Protege contra pérdida de datos añadiendo sufijos incrementales.
#   - Layer Shell Deadlock Fix: Descongela la pantalla inmediatamente tras la captura
#     para evitar que diálogos gráficos (Zenity) queden atrapados.
# =======================================================================================

set -euo pipefail

# Argumento por defecto: Si falla la lectura, asume recorte de área.
MODO="${1:---area}" 
OUT_DIR="$HOME/Imagenes/Capturas"

# Aseguramos que la estructura de directorios del usuario exista
mkdir -p "$OUT_DIR"

# [!] CREACIÓN DEL RECURSO TEMPORAL EN RAM (Prevención de colisiones)
TEMP_FILE="$(mktemp /tmp/captura_temp.XXXXXX.png)"
PICKER_PID=""

# ── 1. EL RECOLECTOR DE BASURA Y PARACAÍDAS (TRAP) ─────────────────────────────────────
cleanup() {
    # 1. Liberar el framebuffer: Aniquilamos el congelador de pantalla si sigue vivo.
    # Se usa kill -0 para preguntar al Kernel si el PID existe antes de matarlo.
    if [[ -n "${PICKER_PID:-}" ]] && kill -0 "$PICKER_PID" 2>/dev/null; then
        kill -9 "$PICKER_PID" 2>/dev/null || true
    fi

    # 2. Limpieza de Memoria RAM: Destruimos el temporal si la captura se canceló.
    if [[ -f "${TEMP_FILE:-}" ]]; then
        rm -f "$TEMP_FILE" 2>/dev/null || true
    fi
}

# Asociamos la limpieza a cualquier salida (éxito, error, o interrupción manual)
trap cleanup EXIT INT TERM HUP


# ── 2. LÓGICA DE CAPTURA (MÁQUINA DE ESTADOS) ──────────────────────────────────────────
case "$MODO" in
    --full)
        grim "$TEMP_FILE"
        ;;

    --monitor)
        # IPC Query: Pregunta a Hyprland qué monitor tiene el ratón encima ahora mismo.
        MONITOR="$(hyprctl monitors -j | jq -r '.[] | select(.focused==true) | .name')"
        grim -o "$MONITOR" "$TEMP_FILE"
        ;;

    --area|--name)
        # 1. CONGELAMIENTO (Freeze):
        # Lanzamos hyprpicker en background para "congelar" la pantalla visualmente.
        hyprpicker -r &
        PICKER_PID=$!
        
        # 2. SINCRONISMO SEGURO (Layer Shell Fix & Fail-Close):
        # NOTA: hyprpicker se dibuja en la capa 'Layer Shell' de Wayland, por lo que 
        # NO aparece en 'hyprctl clients'. Damos 200ms para que la GPU renderice el 
        # congelamiento y verificamos clínicamente que el proceso no haya crasheado.
        sleep 0.2
        if ! kill -0 "$PICKER_PID" 2>/dev/null; then
            notify-send -u critical "Captura Abortada" "El congelador (hyprpicker) falló al iniciar."
            exit 1 # Dispara el TRAP y limpia la RAM
        fi

        # 3. SELECCIÓN DE ÁREA:
        # Timeout de 30s por si el usuario olvida que activó el recorte.
        SELECCION="$(timeout -s 9 30s slurp 2>/dev/null || true)"

        # Aborto seguro: Si se presiona ESC o expira el tiempo.
        if [[ -z "$SELECCION" ]]; then
            exit 0 # Dispara el TRAP automáticamente
        fi

        # 4. CAPTURA DEL ÁREA GEOMÉTRICA:
        grim -g "$SELECCION" "$TEMP_FILE"

        # 5. DESCONGELAMIENTO INMEDIATO (Fix de Deadlock):
        # Una vez que grim toma la foto, hyprpicker ya no es necesario.
        # Es CRUCIAL matarlo aquí, de lo contrario la pantalla seguirá congelada
        # y el diálogo de Zenity (o cualquier otra ventana) quedará atrapado debajo.
        if [[ -n "${PICKER_PID:-}" ]] && kill -0 "$PICKER_PID" 2>/dev/null; then
            kill "$PICKER_PID" 2>/dev/null || true
        fi
        ;;

    *)
        exit 1
        ;;
esac


# ── 3. PROCESAMIENTO FINAL Y PERSISTENCIA ATÓMICA ──────────────────────────────────────
# Verifica que el archivo temporal exista y contenga datos reales (-s)
if [[ -s "$TEMP_FILE" ]]; then
    FECHA="$(date +'%Y%m%d_%H%M%S')"

    if [[ "$MODO" == "--name" ]]; then
        # Petición de nombre mediante interfaz gráfica nativa
        NOMBRE="$(zenity --entry \
            --title="Guardar Captura" \
            --text="Nombre del archivo:" \
            --entry-text="cap_$FECHA" 2>/dev/null)"

        if [[ -z "$NOMBRE" ]]; then
            exit 0 # Cancelado por el usuario
        fi
        
        # [!] SEGURIDAD: Sanitización contra Path Traversal
        # Transforma cualquier barra '/' o espacio en un guion bajo '_'
        # Esto impide que un atacante (o un error) escriba en rutas como ../../etc
        NOMBRE="${NOMBRE//\//_}" 
        NOMBRE="${NOMBRE// /_}"  
        
        DESTINO="$OUT_DIR/${NOMBRE}.png"
    else
        DESTINO="$OUT_DIR/cap_$FECHA.png"
    fi

    # [!] PROTECCIÓN ANTI-SOBREESCRITURA (Anti-Overwrite)
    # Si el archivo ya existe, añade un sufijo incremental (ej. foto_(1).png)
    CONTADOR=1
    BASE_DESTINO="${DESTINO%.png}"
    while [[ -f "$DESTINO" ]]; do
        DESTINO="${BASE_DESTINO}_(${CONTADOR}).png"
        ((CONTADOR++))
    done

    # [!] GUARDADO REALMENTE ATÓMICO (Cross-Device Safe)
    # Mover de RAM (tmpfs) a SSD no es atómico. Copiamos primero con extensión .part,
    # y solo cuando la copia termine exitosamente, lo renombramos a .png (eso sí es atómico).
    TEMP_DESTINO="${DESTINO}.part"
    if cp "$TEMP_FILE" "$TEMP_DESTINO" && mv -f "$TEMP_DESTINO" "$DESTINO"; then
        : # Operación exitosa
    else
        rm -f "$TEMP_DESTINO" 2>/dev/null
        notify-send -u critical "Error de Captura" "Fallo de I/O al escribir en disco." 
        exit 1
    fi
    
    # [!] INYECCIÓN A PORTAPAPELES TOLERANTE A FALLOS
    # Si wl-copy falla o está bloqueado, el '|| true' evita que 'set -e' mate el script,
    # asegurando que la notificación de éxito siempre se muestre.
    wl-copy --type image/png < "$DESTINO" 2>/dev/null || true
    
    notify-send "¡Captura lista!" "Guardada en Imágenes y en portapapeles" --icon="$DESTINO"
fi
