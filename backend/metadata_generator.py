# -*- coding: utf-8 -*-
"""
Metadata Generator - AutoClip-AI

Generates a complete "Publishing Package" (Pacote de Publicação) for YouTube
right after a video is processed:

  - Title suggestions (proven viral formulas, localized to the video language)
  - Full description with: hook, auto-chapters (M:SS), CTA (subscribe/like/
    comment) and hashtag block
  - Hashtags + YouTube tags (the separate tags field)

Two engines, graceful degradation:
  1. Heuristic engine (always available, fully offline):
     keyword extraction (pt/es/en stopword-aware), topic detection via
     domain dictionaries (gaming/ARK, vlog, tutorial...), highlight mining
     from energetic segments ("!", "?", caps) and template slot-filling.
  2. Ollama engine (optional): reuses the analyzer's Ollama helpers to rewrite
     titles and the description with a local LLM. Falls back to heuristic on
     any failure, exactly like the clips pipeline does.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Language packs
# ---------------------------------------------------------------------------
STOPWORDS: set = {
    # pt
    "que", "para", "com", "uma", "não", "mais", "isso", "aqui", "está", "tem",
    "muito", "mas", "porque", "como", "só", "vamos", "ele", "ela", "eles",
    "nós", "vocês", "você", "tudo", "todo", "toda", "ser", "estou", "estava",
    "então", "agora", "também", "vai", "vou", "ver", "dar", "deu", "aí",
    "esse", "essa", "este", "esta", "aquele", "pelo", "pela", "sem", "nos",
    "nas", "nosso", "nossa", "seu", "sua", "meu", "minha", "até", "pode",
    "coisa", "coisas", "olha", "olha", "mira", "tipo", "cara", "gente",
    "mirar", "mirando", "bien", "dónde", "donde", "encontré", "encontrar",
    "encontrado", "tenemos", "tienen", "hacer", "hace", "quiero", "quero",
    "nunca", "siempre", "igual", "poco", "menos", "aqui", "agora", "assim",
    "tambem", "dentro", "contra", "porque", "porquê", "coisa", "olha",
    "valeu", "oba", "olé", "hala", "vamos", "vamos", "bora", "tales",
    "mira", "mira", "veja", "olhem", "aí", "ahí", "entao", "então",
    # es
    "para", "como", "pero", "más", "menos", "esto", "esta", "este", "estos",
    "estas", "aquí", "allí", "allá", "tiene", "tienen", "muy", "mucho",
    "muchos", "porque", "entonces", "ahora", "también", "vamos", "voy", "va",
    "van", "ver", "dar", "dio", "ese", "esa", "eso", "aquel", "por", "sin",
    "nos", "nuestro", "nuestra", "suyo", "hasta", "puede", "cosas", "cosa",
    "mira", "oye", "bueno", "claro", "creo", "sí", "si", "no", "hay", "era",
    "eras", "somos", "soy", "estoy", "está", "estás", "tiene", "cuando",
    "donde", "porque", "qué", "que", "los", "las", "del", "por", "una",
    "uno", "unos", "unas", "les", "le", "lo", "nos", "vos", "usted",
    # en (just in case)
    "the", "and", "for", "that", "this", "with", "have", "are", "was", "you",
}

# Topic dictionaries: keyword -> weight. First topic to reach threshold wins.
TOPIC_DICTS: Dict[str, Dict[str, float]] = {
    "gaming_ark": {
        "dino": 3, "dinos": 3, "dinosaurio": 3, "dinosaurios": 3,
        "dinossauro": 3, "dinossauros": 3, "rex": 2, "trex": 3,
        "grifo": 3, "griffin": 3, "estego": 3, "stego": 3, "trike": 3,
        "parasaurio": 3, "parasauro": 3, "argy": 2, "wyvern": 3,
        "cryopod": 3, "cryopods": 3, "criopod": 3, "giganoto": 3,
        "tribu": 2, "tribo": 2, "torreta": 2, "torreta": 2, "turret": 2,
        "generador": 2, "generador": 2, "gerador": 2, "gasolina": 1,
        "polímero": 2, "polimero": 2, "polímeros": 2, "polymers": 2,
        "planta": 1, "artefacto": 2, "artefactos": 2, "artefato": 2,
        "montura": 2, "montaria": 2, "silla": 1, "saddle": 2,
        "huevo": 2, "huevos": 2, "ovo": 1, "ovos": 1, "fertilizado": 2,
        "spawn": 1, "farm": 1, "farmear": 2, "metal": 1, "pvp": 3,
        "raid": 3, "raideo": 3, "saquear": 2, "saqueamos": 3, "stealqueando": 3,
        "stalkeando": 2, "c4": 2, "explosivo": 1, "brechar": 2, "breach": 2,
        "offline": 1, "base": 1, "bases": 1, "mapa": 1, "server": 1,
        "servidor": 1, "tribemate": 1, "clan": 1,
    },
    "vlog": {
        "familia": 2, "hijos": 2, "esposa": 2, "esposo": 2, "cumpleaños": 3,
        "fiesta": 2, "viaje": 3, "playa": 2, "casa": 1, "aniversario": 3,
        "família": 2, "aniversário": 3, "praia": 2, "festa": 2, "viagem": 3,
    },
    "tutorial": {
        "tutorial": 3, "aprender": 2, "enseñar": 2, "enseño": 3, "paso": 2,
        "paso a paso": 3, "configurar": 2, "instalar": 2, "explicar": 2,
        "aprender": 2, "ensinar": 2, "passo": 2, "configurar": 2, "instalar": 2,
        "explicar": 2, "dica": 2, "dicas": 3, "truque": 2, "truques": 3,
        "truco": 2, "trucos": 3,
    },
}

# Words that mark a strong/energetic segment (good title/description hooks).
ENERGY_MARKERS = ("!", "¿", "¡", "?")

# Coveted in-game items worth naming in a title (topic: gaming_ark).
COOL_ITEMS: Dict[str, str] = {
    "grifo": "GRIFOS", "griffin": "GRIFOS", "estego": "ESTEGOS",
    "stego": "ESTEGOS", "trike": "TRIKES", "rex": "T-REX",
    "wyvern": "WYVERNS", "cryopod": "CRYOPODS", "criopod": "CRYOPODS",
    "c4": "C4", "giganoto": "GIGA", "argy": "ARGY",
}

# Title templates per output language. {slot} values come from the heuristic.
TITLE_TEMPLATES: Dict[str, List[str]] = {
    "gaming_ark": {
        "es": [
            "SAQUEAMOS una base llena de {things} 😱{emoji} | ARK PvP en Español",
            "OFFLINE RAID PERFECTO: {loot} y LOOT INCREÍBLE 💥 | ARK Serie PvP",
            "Espiamos esta base y quedó DESTROYED 💀 | ARK PvP",
            "La base MÁS RICA que hemos saqueado en la serie 🔥 | ARK PvP",
        ],
        "pt": [
            "INVADIMOS uma base CHEIA de {things} 😱{emoji} | ARK PvP",
            "OFFLINE RAID PERFEITO: {loot} e LOOT INCRÍVEL 💥 | ARK Série PvP",
            "Espiamos essa base e ela foi DESTRUÍDA 💀 | ARK PvP",
            "A base MAIS RICA que já invadimos na série 🔥 | ARK PvP",
        ],
    },
    "gaming_generic": {
        "es": [
            "{highlight} 😱 | {topic} Gameplay en Español",
            "ESTO pasó cuando {action} 💥 | {topic}",
            "No vas a CREER lo que encontramos {context} 😱 | {topic}",
            "El mejor momento de la serie hasta ahora 🔥 | {topic}",
        ],
        "pt": [
            "{highlight} 😱 | {topic} Gameplay",
            "ISSO aconteceu quando {action} 💥 | {topic}",
            "Você não vai ACREDITAR no que encontramos {context} 😱 | {topic}",
            "O melhor momento da série até agora 🔥 | {topic}",
        ],
    },
    "vlog": {
        "es": [
            "{highlight} 😱 | Vlog en familia",
            "El día que {action} ❤️ | Vlog",
            "NUESTRO {context} fue increíble 🎉 | Vlog",
            "Así vivimos {context} 🎬 | Vlog en familia",
        ],
        "pt": [
            "{highlight} 😱 | Vlog em família",
            "O dia em que {action} ❤️ | Vlog",
            "NOSSO {context} foi incrível 🎉 | Vlog",
            "Assim vivemos {context} 🎬 | Vlog em família",
        ],
    },
    "tutorial": {
        "es": [
            "{topic}: {action} en {minutes} minutos ⏱️ | Guía completa",
            "El TRUCO que nadie te cuenta sobre {topic} 🤯",
            "Aprende {topic} desde CERO 💡 | Tutorial 2026",
            "{highlight} | {topic} Tips",
        ],
        "pt": [
            "{topic}: {action} em {minutes} minutos ⏱️ | Guia completa",
            "O TRUQUE que ninguém te conta sobre {topic} 🤯",
            "Aprenda {topic} do ZERO 💡 | Tutorial 2026",
            "{highlight} | {topic} Dicas",
        ],
    },
    "generic": {
        "es": [
            "{highlight} 😱",
            "ESTO es lo que pasó {context} 💥",
            "No vas a CREER lo que encontramos {context} 😱",
            "El video más INCREÍBLE del canal hasta ahora 🔥",
        ],
        "pt": [
            "{highlight} 😱",
            "ISSO foi o que aconteceu {context} 💥",
            "Você não vai ACREDITAR no que encontramos {context} 😱",
            "O vídeo mais INCRÍVEL do canal até agora 🔥",
        ],
    },
}

CTA_BLOCKS: Dict[str, str] = {
    "es": (
        "👉 SUSCRÍBETE y activa la campanita 🔔 para no perderte el próximo "
        "video — ¡cada episodio es más épico que el anterior!\n\n"
        "👍 Deja tu LIKE si llegaste hasta el final\n"
        "💬 Comenta lo que más te gustó del video"
    ),
    "pt": (
        "👉 SE INSCREVE no canal e ativa o sininho 🔔 pra não perder o próximo "
        "vídeo — cada episódio é mais épico que o anterior!\n\n"
        "👍 Deixa seu LIKE se você chegou até o final\n"
        "💬 Comenta o que você mais curtiu no vídeo"
    ),
    "en": (
        "👉 SUBSCRIBE and hit the bell 🔔 so you don't miss the next video!\n\n"
        "👍 Leave a LIKE if you made it to the end\n"
        "💬 Comment your favorite part"
    ),
}

HASHTAG_POOLS: Dict[str, Dict[str, List[str]]] = {
    "gaming_ark": {
        "es": ["#ARK", "#ARKSurvivalAscended", "#ARKSurvivalEvolved", "#PvP",
               "#OfflineRaid", "#Raid", "#GameplayEnEspañol", "#Dinosaurios", "#Gaming"],
        "pt": ["#ARK", "#ARKSurvivalAscended", "#ARKSurvivalEvolved", "#PvP",
               "#OfflineRaid", "#Raid", "#ARKemPortugues", "#Dinossauros", "#Gaming"],
    },
    "gaming_generic": {
        "es": ["#Gaming", "#Gameplay", "#GameplayEnEspañol", "#PvP", "#Gamer"],
        "pt": ["#Gaming", "#Gameplay", "#GameplayPT", "#PvP", "#Gamer"],
    },
    "vlog": {
        "es": ["#Vlog", "#Familia", "#Rotina", "#DiaAdia", "#VlogEnFamilia"],
        "pt": ["#Vlog", "#Família", "#Rotina", "#DiaADia", "#VlogEmFamília"],
    },
    "tutorial": {
        "es": ["#Tutorial", "#Guía", "#Tips", "#Aprende", "#HowTo"],
        "pt": ["#Tutorial", "#Guia", "#Dicas", "#Aprenda", "#HowTo"],
    },
    "generic": {
        "es": ["#YouTube", "#Video", "#Viral", "#Contenido"],
        "pt": ["#YouTube", "#Vídeo", "#Viral", "#Conteúdo"],
    },
}

TAG_POOLS: Dict[str, List[str]] = {
    "gaming_ark": [
        "ark pvp", "ark raid", "offline raid", "ark gameplay", "ark español",
        "ark português", "ark dinos", "ark série", "ark tribu", "ark survival",
    ],
    "gaming_generic": ["gameplay", "pvp", "gameplay español", "gameplay português", "gaming"],
    "vlog": ["vlog", "vlog familia", "dia a dia", "rotina"],
    "tutorial": ["tutorial", "guía", "tips", "dicas", "how to"],
    "generic": ["youtube", "entretenimiento", "entretenimento"],
}

TOPIC_LABELS: Dict[str, Dict[str, str]] = {
    "gaming_ark": {"es": "ARK PvP", "pt": "ARK PvP", "en": "ARK PvP"},
    "gaming_generic": {"es": "Gaming", "pt": "Games", "en": "Gaming"},
    "vlog": {"es": "Vlog", "pt": "Vlog", "en": "Vlog"},
    "tutorial": {"es": "Tutorial", "pt": "Tutorial", "en": "Tutorial"},
    "generic": {"es": "YouTube", "pt": "YouTube", "en": "YouTube"},
}

THING_WORDS: Dict[str, Dict[str, str]] = {
    "es": {"dinos": "DINOSAURIOS", "bases": "BASES", "loot": "LOOT"},
    "pt": {"dinos": "DINOSSAUROS", "bases": "BASES", "loot": "LOOT"},
}

# ---------------------------------------------------------------------------
# Heuristic core
# ---------------------------------------------------------------------------
def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-záéíóúãõâêôàçüñ]{4,}", text.lower())


def _top_keywords(text: str, limit: int = 12) -> List[Tuple[str, int]]:
    counts = Counter(t for t in _tokens(text) if t not in STOPWORDS)
    return counts.most_common(limit)


def _detect_topic(text: str) -> str:
    lowered = text.lower()
    scores: Dict[str, float] = {}
    for topic, table in TOPIC_DICTS.items():
        score = 0.0
        for word, weight in table.items():
            hits = lowered.count(word)
            if hits:
                score += weight * min(hits, 5)
        scores[topic] = score
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] < 6.0:  # too weak -> generic
        return "generic"
    return best[0]


def _output_language(transcript_language: Optional[str]) -> str:
    lang = (transcript_language or "pt").lower()
    return lang if lang in ("pt", "es", "en") else "pt"


def _highlights(segments: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """Energetic segments - exclamation/question marks, caps or profanity."""
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for seg in segments:
        text = seg.get("text", "")
        if len(text) < 12 or len(text) > 90:
            continue
        score = 0.0
        score += text.count("!") * 2.0
        score += text.count("¿") + text.count("¡") + text.count("?")
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            score += caps_ratio * 3.0
        if score <= 0:
            continue
        scored.append((score, seg))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [seg for _score, seg in scored[:limit]]


def _clean_title_slot(text: str, max_words: int = 8) -> str:
    """Turns a segment into a punchy title fragment (no trailing punctuation)."""
    text = re.sub(r"\s+", " ", text).strip(" ¡!¿?.,")
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])
    return text.strip()


def _format_chapter_time(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _heuristic_titles(
    topic: str, lang: str, keywords: List[Tuple[str, int]],
    highlights: List[Dict[str, Any]], duration: float,
) -> List[str]:
    templates = TITLE_TEMPLATES.get(topic, TITLE_TEMPLATES["generic"])[lang]
    kw = [k for k, _c in keywords]

    # Prefer real coveted items found in the transcript over generic slots.
    found_items = [
        label for word, label in COOL_ITEMS.items()
        if any(k == word or k.startswith(word) for k in kw)
    ]
    things = THING_WORDS[lang].get("dinos", kw[0].upper() if kw else "LOOT")
    # NOTE: the "{loot} y LOOT INCREIBLE" template already appends "y LOOT",
    # so the slot itself must contain only the item names.
    loot_slot = ", ".join(found_items[:2]) if found_items else (
        ", ".join(kw[:2]).upper() if kw else "C4"
    )
    highlight = _clean_title_slot(highlights[0]["text"]) if highlights else ""
    action = _clean_title_slot(highlights[1]["text"], 6) if len(highlights) > 1 else (
        _clean_title_slot(keywords and " ".join(kw[:2]) or "empezó la acción")
    )
    context = "en este video"
    minutes = max(2, int(duration // 60))
    topic_label = TOPIC_LABELS.get(topic, TOPIC_LABELS["generic"])[lang]
    emoji = "🦖" if topic == "gaming_ark" else "🔥"

    slots = {
        "things": things, "loot": loot_slot, "highlight": highlight,
        "action": action, "context": context, "minutes": str(minutes),
        "topic": topic_label, "emoji": emoji,
    }
    titles: List[str] = []
    for template in templates:
        try:
            title = template.format(**slots)
        except (KeyError, IndexError):
            title = template
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) <= 100 and title not in titles:
            titles.append(title)
    return titles[:4]


def _heuristic_description(
    topic: str, lang: str, keywords: List[Tuple[str, int]],
    highlights: List[Dict[str, Any]], chapters: List[Dict[str, Any]],
    first_segments: List[Dict[str, Any]],
) -> str:
    kw = [k for k, _c in keywords[:6]]
    kw_str = ", ".join(kw) if kw else "..."
    hook_lines: List[str] = []

    if first_segments:
        opening = " ".join(seg["text"] for seg in first_segments[:2]).strip()
        opening = re.sub(r"\s+", " ", opening)
        if len(opening) > 220:
            opening = opening[:217].rstrip() + "..."
        hook_lines.append(opening)
    if highlights:
        best = highlights[0]["text"].strip()
        hook_lines.append(f'💥 Momento fuerte: "{best}"')
    hook_lines.append(f"🎯 En este video: {kw_str}." if lang == "es"
                      else f"🎯 Nesse vídeo: {kw_str}.")

    parts: List[str] = ["\n".join(hook_lines)]

    if chapters:
        chapter_block = "⏱️ CAPÍTULOS:\n" + "\n".join(
            f"{_format_chapter_time(ch['start'])} {ch.get('title') or 'Capítulo'}"
            for ch in chapters
        )
        parts.append(chapter_block)

    parts.append(CTA_BLOCKS.get(lang, CTA_BLOCKS["pt"]))
    return "\n\n".join(parts)


def _hashtags(topic: str, lang: str, limit: int = 8) -> List[str]:
    pool = HASHTAG_POOLS.get(topic, HASHTAG_POOLS["generic"])[lang]
    return pool[:limit]


def _tags(topic: str, keywords: List[Tuple[str, int]], limit: int = 12) -> List[str]:
    base = list(TAG_POOLS.get(topic, TAG_POOLS["generic"]))
    for kw, _c in keywords[:4]:
        if kw not in base:
            base.append(kw)
    return base[:limit]


# ---------------------------------------------------------------------------
# Ollama enhancement (optional - reuses analyzer helpers)
# ---------------------------------------------------------------------------
def _ollama_titles_and_description(
    transcript_text: str, language: str, chapters: List[Dict[str, Any]],
    model_name: str,
) -> Optional[Dict[str, Any]]:
    try:
        from .analyzer import _ollama_available, _ollama_generate
    except ImportError:
        return None
    if not _ollama_available():
        return None

    excerpt = transcript_text[:4000]
    chapters_str = "\n".join(
        f"{_format_chapter_time(ch['start'])} {ch.get('title') or ''}" for ch in chapters
    ) or "(sin capítulos)"

    lang_name = {"pt": "português", "es": "español", "en": "english"}.get(language, "português")
    prompt = (
        f"Eres un experto en SEO de YouTube. Basándote en la transcripción de un video "
        f"(idioma: {lang_name}), genera un paquete de publicación.\n\n"
        f"TRANSCRIPCIÓN (extracto):\n{excerpt}\n\n"
        f"CAPÍTULOS:\n{chapters_str}\n\n"
        f"Responde SOLO con JSON válido con estas claves:\n"
        f'{{"titles": [4 títulos virales de YouTube en {lang_name}, máximo 90 caracteres, '
        f'con 1-2 emojis, estilo curiosity gap], '
        f'"description": "descripción completa en {lang_name} con gancho inicial, luego la '
        f'lista de capítulos exactamente como fue dada, luego llamada a suscribirse, dar like '
        f'y comentar", '
        f'"hashtags": [6-8 hashtags en {lang_name}]}}'
    )
    raw = _ollama_generate(prompt, model_name, timeout=180.0)
    if not raw:
        return None

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    titles = parsed.get("titles") or []
    description = parsed.get("description") or ""
    hashtags = parsed.get("hashtags") or []
    if not titles or not description:
        return None
    return {
        "titles": [str(t).strip() for t in titles if str(t).strip()][:5],
        "description": str(description).strip(),
        "hashtags": [str(h).strip() for h in hashtags if str(h).strip()][:8],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_video_metadata(
    transcript: Dict[str, Any],
    chapters: Optional[List[Dict[str, Any]]] = None,
    duration: float = 0.0,
    video_title: Optional[str] = None,
    use_ollama: bool = False,
    ollama_model: str = "llama3",
    output_language: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds the full publishing package for a processed video.

    output_language: "pt"/"es"/"en" forces the package language; None/"auto"
    follows the language detected in the transcript.

    Returns a dict with:
      titles (list[str]), description (str), hashtags (list[str]),
      tags (list[str]), topic, language, source ("ollama"|"heuristic"),
      copy_paste_block (str) - ready-to-paste YouTube block.
    """
    chapters = chapters or []
    segments: List[Dict[str, Any]] = transcript.get("segments", []) or []
    full_text: str = transcript.get("full_text", "") or ""
    requested = (output_language or "").strip().lower()
    if requested in ("pt", "es", "en"):
        language = requested  # user override wins
    else:
        language = _output_language(transcript.get("language"))

    keywords = _top_keywords(full_text)
    topic = _detect_topic(full_text)
    highlights = _highlights(segments)
    first_segments = segments[:3]

    titles = _heuristic_titles(topic, language, keywords, highlights, duration)
    description = _heuristic_description(
        topic, language, keywords, highlights, chapters, first_segments
    )
    hashtags = _hashtags(topic, language)
    tags = _tags(topic, keywords)
    source = "heuristic"

    if use_ollama and full_text:
        llm = _ollama_titles_and_description(full_text, language, chapters, ollama_model)
        if llm:
            titles = llm["titles"] or titles
            description = llm["description"] or description
            hashtags = llm["hashtags"] or hashtags
            source = "ollama"

    # Append hashtags to the description (YouTube convention: last block).
    if hashtags:
        description = f"{description}\n\n{' '.join(hashtags)}"

    # Ensure the chapters appear in the description even if the LLM dropped them.
    if chapters and "CAPÍTULOS" not in description.upper():
        block = "⏱️ CAPÍTULOS:\n" + "\n".join(
            f"{_format_chapter_time(ch['start'])} {ch.get('title') or 'Capítulo'}"
            for ch in chapters
        )
        description = f"{description}\n\n{block}"

    recommended = titles[0] if titles else (video_title or "Novo vídeo no canal!")
    copy_paste_block = (
        f"🎬 TÍTULO SUGERIDO:\n{recommended}\n\n"
        + "\n".join(f"   {i + 1}. {t}" for i, t in enumerate(titles))
        + f"\n\n📝 DESCRIÇÃO (copiar e colar):\n\n{description}\n\n"
        + f"🏷️ TAGS (campo de tags do YouTube):\n{', '.join(tags)}\n"
    )

    return {
        "titles": titles,
        "description": description,
        "hashtags": hashtags,
        "tags": tags,
        "topic": topic,
        "language": language,
        "source": source,
        "copy_paste_block": copy_paste_block,
    }
