#!/bin/bash

# Si se recibe el argumento "toggle", cambia el estado
if [[ "$1" == "toggle" ]]; then
    if bluetoothctl show | grep -q "Powered: yes"; then
        bluetoothctl power off
    else
        sudo /usr/sbin/rfkill unblock bluetooth
        bluetoothctl power on
    fi
fi

# Salida para Waybar
if bluetoothctl show | grep -q "Powered: yes"; then
    # Estado: Activo (Azul)
    echo '{"text": "", "class": "active", "tooltip": "Bluetooth Encendido"}'
else
    # Estado: Inactivo (Rojo)
    echo '{"text": "", "class": "inactive", "tooltip": "Bluetooth Apagado"}'
fi
