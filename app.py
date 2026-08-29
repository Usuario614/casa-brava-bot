"""
Casa Brava - Bot de reservas
=============================
Este es el punto de entrada de la app. Tiene:

1. Un SIMULADOR web (/) donde vos escribís un mensaje como si fueras
   un cliente por WhatsApp, y el bot te contesta en pantalla.
2. Un WEBHOOK real (/webhook) conectado a la API oficial de WhatsApp
   (Meta): verifica la suscripción, recibe los mensajes entrantes de
   los clientes y les manda la respuesta del bot de vuelta.

Los dos caminos (simulador y webhook real) llaman a la MISMA función
`procesar_mensaje()` de bot_logic.py, así que todo lo que probemos acá
con el simulador funciona igual cuando conectemos WhatsApp de verdad.
"""

import os
import uuid

import requests
from flask import Flask, render_template, request, session, jsonify

from bot_logic import procesar_mensaje

app = Flask(__name__)

# --- Datos de conexión con Meta (WhatsApp Cloud API) ---
# Los 3 de abajo son los que anotamos en la sesión con Meta.
# Lo ideal es moverlos a variables de entorno en Render más adelante
# (por eso usamos os.environ.get con estos valores como default por ahora).
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "casabrava2026secreto")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")  # el token que generaste en Meta (dura 24hs si es temporal)
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "1202645839607745")  # el de prueba; cambialo cuando conectes el número real
GRAPH_API_VERSION = "v21.0"
# Necesaria para que Flask pueda usar cookies de sesión (así cada
# navegador/pestaña que prueba el simulador tiene su propia charla
# con el bot, sin mezclarse con la de otra persona probando al mismo
# tiempo). No es sensible, es solo para firmar la cookie.
app.secret_key = "casa-brava-dev-secret"


@app.route("/", methods=["GET"])
def simulador():
    """Muestra el formulario donde probamos el bot a mano."""
    return render_template("simulador.html", respuesta=None)


@app.route("/simular", methods=["POST"])
def simular():
    """
    Recibe lo que escribiste en el formulario, lo pasa por el mismo
    'cerebro' (bot_logic.procesar_mensaje) que usará WhatsApp de
    verdad, y te muestra la respuesta.
    """
    nombre = request.form.get("nombre", "").strip() or "Cliente"
    mensaje = request.form.get("mensaje", "").strip()

    if not mensaje:
        return render_template(
            "simulador.html", respuesta="⚠ Escribí algún mensaje para probar."
        )

    # session_id propio por navegador, para que la conversación con
    # slots pendientes (fecha/noches/personas) seguidas de bot_logic
    # se mantenga entre un mensaje y el siguiente.
    if "sim_id" not in session:
        session["sim_id"] = str(uuid.uuid4())
    session_id = session["sim_id"]

    respuesta = procesar_mensaje(mensaje, session_id, nombre)
    return render_template("simulador.html", respuesta=respuesta, ultimo_mensaje=mensaje, ultimo_nombre=nombre)


def enviar_mensaje_whatsapp(numero_destino: str, texto: str) -> None:
    """
    Le pide a la API de Meta que mande `texto` por WhatsApp al número
    `numero_destino` (formato internacional sin '+', ej: 5522999999999).
    """
    if not WHATSAPP_TOKEN:
        print("[aviso] WHATSAPP_TOKEN no está configurado, no se pudo enviar la respuesta.")
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f"[error] Meta devolvió {resp.status_code}: {resp.text}")
    except requests.RequestException as error:
        print(f"[error] No se pudo mandar el mensaje por WhatsApp: {error}")


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """
    Meta llama a esta URL con un GET cuando activás/guardás la
    configuración del webhook, para confirmar que el token coincide.
    """
    modo = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if modo == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Token de verificación inválido", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    """
    Acá llegan los mensajes reales de WhatsApp. Sacamos el texto y el
    número del cliente, se lo pasamos al mismo 'cerebro' del bot que
    usa el simulador, y mandamos la respuesta de vuelta por WhatsApp.
    """
    data = request.get_json(silent=True) or {}

    try:
        entry = data.get("entry", [])[0]
        cambio = entry.get("changes", [])[0]
        valor = cambio.get("value", {})
        mensajes = valor.get("messages")

        if not mensajes:
            # Puede ser una notificación de estado (entregado, leído, etc.),
            # no un mensaje nuevo. No hay nada que responder.
            return jsonify({"status": "sin mensajes nuevos"}), 200

        mensaje = mensajes[0]
        numero_cliente = mensaje.get("from")  # ej: "5522999999999"
        texto_cliente = mensaje.get("text", {}).get("body", "")

        contactos = valor.get("contacts", [])
        nombre_cliente = contactos[0].get("profile", {}).get("name", "Cliente") if contactos else "Cliente"

        if not texto_cliente or not numero_cliente:
            return jsonify({"status": "mensaje sin texto (imagen/audio/etc, no soportado aún)"}), 200

        respuesta = procesar_mensaje(texto_cliente, numero_cliente, nombre_cliente)
        enviar_mensaje_whatsapp(numero_cliente, respuesta)

        return jsonify({"status": "procesado"}), 200

    except (IndexError, AttributeError, KeyError) as error:
        print(f"[error] No se pudo interpretar el webhook de Meta: {error} — data: {data}")
        return jsonify({"status": "error al interpretar el mensaje"}), 200


if __name__ == "__main__":
    # debug=True hace que el servidor se reinicie solo cuando guardás
    # cambios en el código, y te muestra errores detallados en el navegador.
    app.run(debug=True, port=5000)
