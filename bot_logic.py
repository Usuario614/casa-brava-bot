"""
Casa Brava - Cerebro del bot
=============================
Acá vive TODA la inteligencia del bot: detectar en qué idioma escribe
el cliente, entender qué actividad quiere, ir pidiéndole los datos que
faltan (fecha, personas, noches) y, cuando ya está todo, avisarle a
n8n para que:
  1) guarde la reserva en Google Sheets
  2) si es Hospedagem, bloquee esas fechas en Google Calendar
     (y automáticamente el calendario de la landing las va a mostrar
     como ocupadas, porque la landing ya lee ese mismo calendario)

Tanto el simulador (/simular) como el webhook real de WhatsApp
(/webhook) llaman a la misma función `procesar_mensaje()`, así que
todo lo que probemos acá funciona igual en los dos lados.

IMPORTANTE - limitación de esta primera versión:
Las conversaciones se guardan en un diccionario en memoria (SESSIONS).
Esto significa que si reiniciás el servidor, se pierden las
conversaciones que estaban a mitad de camino. Para una casa chica como
Casa Brava esto no es grave (en el peor caso el cliente escribe de
nuevo), pero si más adelante querés que sea más robusto, se puede
guardar el estado en un archivo o una base de datos chica.
"""

import os
import random
import re
import string
from datetime import datetime

import requests

# ==========================================================
# 1) CONFIGURACIÓN
# ==========================================================

# URL del webhook de n8n que guarda la reserva en Sheets/Calendar.
# Es LA MISMA url que se pega en la landing en N8N_WEBHOOK_URL.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

# ==========================================================
# 2) DETECCIÓN DE IDIOMA (ES / EN / PT)
# ==========================================================
# Para mensajes cortos de WhatsApp, un detector "inteligente" tipo
# librería de IA a veces se confunde. Por eso usamos una lista de
# palabras muy típicas de cada idioma: si el mensaje tiene alguna,
# ya sabemos con bastante certeza en qué idioma está.

PALABRAS_PT = {
    "olá", "ola", "oi", "voce", "você", "obrigado", "obrigada", "quero",
    "gostaria", "quanto", "quarto", "hospedagem", "passeio", "vaga",
    "disponibilidade", "praia", "não", "nao", "por favor", "bom dia",
    "boa tarde", "boa noite", "reservar", "pessoas", "noites",
}
PALABRAS_ES = {
    "hola", "gracias", "quiero", "quisiera", "cuánto", "cuanto",
    "habitación", "habitacion", "hospedaje", "paseo", "disponibilidad",
    "reservar", "personas", "noches", "buenas", "buenos días",
    "buenas tardes", "buenas noches", "por favor",
}
PALABRAS_EN = {
    "hello", "hi", "thanks", "thank you", "please", "would like",
    "room", "stay", "availability", "book", "booking", "people",
    "nights", "good morning", "good afternoon", "good evening",
}


def detectar_idioma(texto: str, idioma_previo: str = None) -> str:
    """
    Devuelve 'es', 'en' o 'pt'. Si no se detecta nada claro y ya
    veníamos hablando en un idioma con este cliente, seguimos en ese
    idioma. Si es la primera vez y no hay pistas, usamos español por
    defecto (podés cambiarlo a "pt" si la mayoría de tus clientes
    escriben en portugués).
    """
    texto_normalizado = texto.lower()

    puntos_pt = sum(1 for palabra in PALABRAS_PT if palabra in texto_normalizado)
    puntos_es = sum(1 for palabra in PALABRAS_ES if palabra in texto_normalizado)
    puntos_en = sum(1 for palabra in PALABRAS_EN if palabra in texto_normalizado)

    puntajes = {"pt": puntos_pt, "es": puntos_es, "en": puntos_en}
    mejor_idioma = max(puntajes, key=puntajes.get)

    if puntajes[mejor_idioma] == 0:
        return idioma_previo or "es"

    return mejor_idioma


# ==========================================================
# 3) SERVICIOS OFRECIDOS
# ==========================================================
# OJO: las claves ("Hospedagem", "Yoga/Acroyoga", etc.) tienen que
# ser EXACTAMENTE iguales a los valores de data-service en la landing
# y al campo "servicio" que le mandamos a n8n. Así todo queda
# ordenado igual en la web, en el bot y en el Google Sheet.

SERVICIOS = {
    "Hospedagem": {
        "keywords": {
            "pt": ["hospedagem", "quarto", "dormir", "pernoite", "vaga"],
            "es": ["hospedaje", "habitación", "habitacion", "alojamiento", "dormir"],
            "en": ["stay", "room", "accommodation", "sleep"],
        },
        "slots": ["fecha", "noches", "personas"],
    },
    "Yoga/Acroyoga": {
        "keywords": {
            "pt": ["yoga", "acroyoga", "aula"],
            "es": ["yoga", "acroyoga", "clase"],
            "en": ["yoga", "acroyoga", "class"],
        },
        "slots": ["fecha", "personas"],
    },
    "Trilhas": {
        "keywords": {
            "pt": ["trilha", "trilhas", "caminhada", "mirante"],
            "es": ["trilha", "trekking", "caminata", "mirador"],
            "en": ["hike", "hiking", "trail", "trek"],
        },
        "slots": ["fecha", "personas"],
    },
    "Passeio de barco": {
        "keywords": {
            "pt": ["barco", "passeio de barco", "piscina natural"],
            "es": ["barco", "paseo en barco", "piscina natural"],
            "en": ["boat", "boat trip"],
        },
        "slots": ["fecha", "personas"],
    },
    "Buggy": {
        "keywords": {
            "pt": ["buggy", "duna", "dunas"],
            "es": ["buggy", "duna", "dunas"],
            "en": ["buggy", "dune", "dunes"],
        },
        "slots": ["fecha", "personas"],
    },
}


def detectar_servicio(texto: str, idioma: str) -> str | None:
    """Busca en el mensaje palabras clave de cada servicio."""
    texto_normalizado = texto.lower()
    for nombre_servicio, info in SERVICIOS.items():
        for palabra in info["keywords"].get(idioma, []):
            if palabra in texto_normalizado:
                return nombre_servicio
    return None


# ==========================================================
# 4) TEXTOS DEL BOT (uno por idioma)
# ==========================================================

TEXTOS = {
    "es": {
        "menu": (
            "¡Hola! Soy el asistente de Casa Brava 🌊. Puedo ayudarte con: "
            "Hospedaje, Yoga/Acroyoga, Trilhas, Paseo en barco o Buggy. "
            "¿Cuál te interesa?"
        ),
        "confirmo_servicio": "¡Genial! Te ayudo a coordinar: {servicio}.",
        "pedir_fecha": "¿Para qué fecha te gustaría? (por ejemplo: 10/09)",
        "pedir_noches": "¿Cuántas noches?",
        "pedir_personas": "¿Cuántas personas son?",
        "resumen": (
            "¡Perfecto, {nombre}! Anoté tu solicitud:\n"
            "Servicio: {servicio}\n{detalle}"
            "Código de reserva: {codigo}\n"
            "En breve Sebastián te confirma la disponibilidad. ¡Gracias! 🙌"
        ),
    },
    "pt": {
        "menu": (
            "Olá! Sou o assistente da Casa Brava 🌊. Posso te ajudar com: "
            "Hospedagem, Yoga/Acroyoga, Trilhas, Passeio de barco ou Buggy. "
            "Qual te interessa?"
        ),
        "confirmo_servicio": "Ótimo! Vou te ajudar a organizar: {servicio}.",
        "pedir_fecha": "Para que data você gostaria? (exemplo: 10/09)",
        "pedir_noches": "Quantas noites?",
        "pedir_personas": "Quantas pessoas?",
        "resumen": (
            "Perfeito, {nombre}! Anotei seu pedido:\n"
            "Serviço: {servicio}\n{detalle}"
            "Código da reserva: {codigo}\n"
            "Em breve o Sebastián confirma a disponibilidade. Obrigado! 🙌"
        ),
    },
    "en": {
        "menu": (
            "Hi! I'm the Casa Brava assistant 🌊. I can help you with: "
            "Stay, Yoga/Acroyoga, Hikes, Boat trip or Buggy. "
            "Which one are you interested in?"
        ),
        "confirmo_servicio": "Great! Let's set up: {servicio}.",
        "pedir_fecha": "What date would you like? (e.g. 09/10)",
        "pedir_noches": "How many nights?",
        "pedir_personas": "How many people?",
        "resumen": (
            "Perfect, {nombre}! I've noted your request:\n"
            "Service: {servicio}\n{detalle}"
            "Booking code: {codigo}\n"
            "Sebastián will confirm availability shortly. Thanks! 🙌"
        ),
    },
}

PREGUNTA_POR_SLOT = {
    "fecha": "pedir_fecha",
    "noches": "pedir_noches",
    "personas": "pedir_personas",
}


# ==========================================================
# 5) ESTADO DE LA CONVERSACIÓN (memoria en RAM, ver aviso arriba)
# ==========================================================
# La "session_id" es el número de teléfono cuando viene de WhatsApp
# real, o el nombre que puso la persona cuando es el simulador.

SESSIONS: dict[str, dict] = {}


def _nueva_sesion() -> dict:
    return {"idioma": None, "servicio": None, "datos": {}, "slots_pendientes": []}


def _generar_codigo_reserva() -> str:
    """
    Genera un código corto tipo ARR-8F42K, para que el cliente tenga
    algo concreto para identificar su reserva (y vos puedas buscarla
    rápido en el Sheet). No hace falta que sea único a nivel mundial,
    alcanza con que no se repita seguido entre tus reservas.
    """
    caracteres = string.ascii_uppercase + string.digits
    sufijo = "".join(random.choices(caracteres, k=5))
    return f"ARR-{sufijo}"


def _extraer_numero(texto: str) -> str:
    """Saca el primer número que encuentre en el texto (para personas/noches)."""
    coincidencia = re.search(r"\d+", texto)
    return coincidencia.group(0) if coincidencia else texto.strip()


# Sugerencia de otra actividad, según lo que ya reservó. No es una
# recomendación "inteligente": es una regla fija simple, pero cumple
# el mismo objetivo (que el cliente sume una segunda experiencia)
# sin necesitar ninguna lógica compleja.
CROSS_SELL = {
    "Hospedagem": "Buggy",
    "Yoga/Acroyoga": "Passeio de barco",
    "Trilhas": "Buggy",
    "Passeio de barco": "Trilhas",
    "Buggy": "Passeio de barco",
}

TEXTO_CROSS_SELL = {
    "es": "Tip: mucha gente combina {servicio} con {sugerido}. ¿Te interesa sumarlo?",
    "pt": "Dica: muita gente combina {servicio} com {sugerido}. Quer incluir também?",
    "en": "Tip: lots of people combine {servicio} with {sugerido}. Want to add it too?",
}


LABELS_DETALLE = {
    "es": {"fecha": "Fecha", "noches": "Noches", "personas": "Personas"},
    "pt": {"fecha": "Data", "noches": "Noites", "personas": "Pessoas"},
    "en": {"fecha": "Date", "noches": "Nights", "personas": "People"},
}


def _armar_detalle(servicio: str, datos: dict, idioma: str) -> str:
    labels = LABELS_DETALLE[idioma]
    partes = []
    if datos.get("fecha"):
        partes.append(f"{labels['fecha']}: {datos['fecha']}")
    if servicio == "Hospedagem" and datos.get("noches"):
        partes.append(f"{labels['noches']}: {datos['noches']}")
    if datos.get("personas"):
        partes.append(f"{labels['personas']}: {datos['personas']}")
    return ("\n".join(partes) + "\n") if partes else ""


def _avisar_a_n8n(nombre: str, telefono: str, servicio: str, datos: dict, idioma: str, codigo: str):
    """
    Le manda la reserva a n8n para que la guarde en Sheets (y bloquee
    el calendario si corresponde). Si falla, no rompe la charla con
    el cliente: solo lo avisamos en la consola del servidor.
    """
    if not N8N_WEBHOOK_URL:
        print("[aviso] N8N_WEBHOOK_URL no está configurada, no se guardó la reserva.")
        return

    payload = {
        "name": nombre,
        "phone": telefono,
        "service": servicio,
        "date": datos.get("fecha", ""),
        "nights": datos.get("noches", "1"),
        "people": datos.get("personas", ""),
        "message": "",
        "lang": idioma,
        "canal": "whatsapp_bot",
        "code": codigo,
    }
    try:
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=5)
    except requests.RequestException as error:
        print(f"[aviso] No se pudo avisar a n8n: {error}")


# ==========================================================
# 6) FUNCIÓN PRINCIPAL: procesar_mensaje
# ==========================================================

def procesar_mensaje(texto_cliente: str, session_id: str, nombre_cliente: str = "Cliente") -> str:
    """
    Punto de entrada único del bot. Recibe el mensaje del cliente y
    devuelve la respuesta.

    session_id: identifica la conversación (teléfono en WhatsApp real,
    nombre en el simulador).
    """
    sesion = SESSIONS.setdefault(session_id, _nueva_sesion())

    sesion["idioma"] = detectar_idioma(texto_cliente, sesion["idioma"])
    idioma = sesion["idioma"]
    textos = TEXTOS[idioma]

    # --- Caso 1: estamos esperando que responda un dato puntual ---
    if sesion["slots_pendientes"]:
        slot_actual = sesion["slots_pendientes"][0]
        valor = texto_cliente.strip()
        if slot_actual in ("noches", "personas"):
            valor = _extraer_numero(texto_cliente)
        sesion["datos"][slot_actual] = valor
        sesion["slots_pendientes"].pop(0)

        if sesion["slots_pendientes"]:
            siguiente_slot = sesion["slots_pendientes"][0]
            return textos[PREGUNTA_POR_SLOT[siguiente_slot]]

        # Ya juntamos todos los datos -> avisamos a n8n y cerramos
        codigo = _generar_codigo_reserva()
        detalle = _armar_detalle(sesion["servicio"], sesion["datos"], idioma)
        respuesta = textos["resumen"].format(
            nombre=nombre_cliente, servicio=sesion["servicio"], detalle=detalle, codigo=codigo
        )

        sugerido = CROSS_SELL.get(sesion["servicio"])
        if sugerido:
            respuesta += "\n\n" + TEXTO_CROSS_SELL[idioma].format(
                servicio=sesion["servicio"], sugerido=sugerido
            )

        _avisar_a_n8n(nombre_cliente, session_id, sesion["servicio"], sesion["datos"], idioma, codigo)
        SESSIONS[session_id] = _nueva_sesion()  # listo para una próxima consulta
        SESSIONS[session_id]["idioma"] = idioma
        return respuesta

    # --- Caso 2: todavía no sabemos qué servicio quiere ---
    servicio = detectar_servicio(texto_cliente, idioma)
    if not servicio:
        return textos["menu"]

    sesion["servicio"] = servicio
    sesion["slots_pendientes"] = list(SERVICIOS[servicio]["slots"])
    primer_slot = sesion["slots_pendientes"][0]

    return (
        textos["confirmo_servicio"].format(servicio=servicio)
        + " "
        + textos[PREGUNTA_POR_SLOT[primer_slot]]
    )
