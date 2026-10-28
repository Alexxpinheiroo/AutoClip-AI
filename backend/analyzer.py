"""Clip selection, virality scoring and post metadata.

This module is the "brain" of AutoClip. It combines:

* transcript hooks / keyword density / pacing;
* local multimodal signals (audio energy + scene cuts) from
  :mod:`backend.media_signals`;
* an optional natural-language prompt (ClipAnything-style);
* an optional local LLM through Ollama for ranking and copywriting.

Everything degrades gracefully: if Ollama or the media signals are missing, the
heuristic engine still produces complete clips with score breakdowns.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from . import media_signals

VIRAL_HOOK_KEYWORDS_PT = [
    "segredo", "erro", "nunca", "sempre", "como", "dinheiro", "lucro", "fórmula",
    "estratégia", "atenção", "cuidado", "verdade", "mentira", "ninguém", "você",
    "por que", "qual", "melhor", "pior", "rápido", "fácil", "difícil", "hack",
    "dica", "regra", "método", "resultado", "sucesso", "fracasso", "mudou", "vida",
]

VIRAL_HOOK_KEYWORDS_EN = [
    "secret", "mistake", "never", "always", "how to", "money", "profit", "formula",
    "strategy", "watch out", "truth", "lie", "nobody", "you", "why", "best",
    "worst", "fast", "easy", "hack", "tip", "rule", "result", "success",
]

EMOTION_WORDS = [
    "incrível", "absurdo", "insano", "chocante", "inacreditável", "medo", "ódio",
    "amor", "raiva", "explodiu", "bombou", "gigante", "enorme", "brutal", "louco",
    "hilário", "engraçado", "melhor", "pior", "nunca", "sempre", "impossível",
    "amazing", "crazy", "insane", "shocking", "unbelievable", "hate", "love",
    "angry", "huge", "best", "worst", "never", "always", "impossible",
]

CONCLUSION_WORDS = [
    "então", "por isso", "no final", "resultado", "conclusão", "aprendi", "moral",
    "resumindo", "ou seja", "therefore", "so that", "in the end", "the result",
    "lesson", "conclusion",
]

GENRE_PRESETS: Dict[str, Dict[str, Any]] = {
    "auto": {
        "label": "Automático",
        "keywords": [],
        "hashtags": [],
    },
    "viral": {
        "label": "Viral geral",
        "keywords": VIRAL_HOOK_KEYWORDS_PT + VIRAL_HOOK_KEYWORDS_EN,
        "hashtags": ["viral", "shorts", "fyp"],
    },
    "humor": {
        "label": "Humor",
        "keywords": [
            "risada", "rindo", "engraçado", "piada", "hilário", "kkk", "haha",
            "comédia", "zoeira", "meme", "funny", "joke", "laugh", "lol",
        ],
        "hashtags": ["humor", "comedia", "funny"],
    },
    "educacional": {
        "label": "Educacional",
        "keywords": [
            "como", "dica", "aprenda", "passo", "método", "explico", "entenda",
            "tutorial", "aula", "segredo", "truque", "how to", "learn", "tip",
            "step", "explain", "tutorial",
        ],
        "hashtags": ["aprenda", "dicas", "educacao"],
    },
    "entrevista": {
        "label": "Entrevista / Podcast",
        "keywords": [
            "história", "aconteceu", "quando eu", "experiência", "aprendi",
            "verdade", "confissão", "podcast", "entrevista", "story", "happened",
            "experience", "learned", "truth", "interview",
        ],
        "hashtags": ["podcast", "entrevista", "historia"],
    },
    "financas": {
        "label": "Finanças / Negócios",
        "keywords": [
            "dinheiro", "lucro", "faturamento", "receita", "investimento",
            "riqueza", "empresa", "negócio", "mercado", "milhão", "mil",
            "money", "profit", "revenue", "investment", "business", "market",
        ],
        "hashtags": ["financas", "negocios", "dinheiro"],
    },
    "fitness": {
        "label": "Fitness / Saúde",
        "keywords": [
            "treino", "emagrecer", "músculo", "dieta", "saúde", "corpo",
            "academia", "proteína", "resultado", "workout", "diet", "health",
            "muscle", "gym", "protein",
        ],
        "hashtags": ["fitness", "saude", "treino"],
    },
    "games": {
        "label": "Games",
        "keywords": [
            "jogo", "jogada", "gameplay", "gol", "vitória", "derrota", "clutch",
            "insano", "partida", "campeonato", "game", "play", "win", "clutch",
            "match", "championship",
        ],
        "hashtags": ["games", "gameplay", "gamer"],
    },
    "noticias": {
        "label": "Notícias / Polêmica",
        "keywords": [
            "notícia", "urgente", "polêmica", "aconteceu", "revelou", "denúncia",
            "governo", "crise", "news", "breaking", "controversy", "revealed",
        ],
        "hashtags": ["noticias", "atualidades", "polemica"],
    },
    "esportes": {
        "label": "Esportes",
        "keywords": [
            "gol", "jogo", "campeonato", "vitória", "derrota", "jogada", "time",
            "atleta", "recorde", "goal", "match", "championship", "win", "player",
        ],
        "hashtags": ["esportes", "futebol", "sports"],
    },
}

STOPWORDS = {
    # PT
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos",
    "das", "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem",
    "que", "se", "e", "ou", "mas", "como", "quando", "onde", "porque", "porquê",
    "eu", "tu", "ele", "ela", "nós", "vocês", "eles", "elas", "me", "te", "lhe",
    "nos", "vos", "isso", "isto", "aquilo", "esse", "essa", "este", "esta",
    "aquele", "aquela", "meu", "minha", "seu", "sua", "nosso", "nossa", "é",
    "são", "foi", "era", "ser", "estar", "está", "estão", "tem", "têm", "há",
    "vai", "vão", "mais", "menos", "muito", "muita", "muitos", "muitas", "já",
    "ainda", "também", "só", "aqui", "ali", "lá", "então", "tipo", "coisa",
    "bem", "mal", "sim", "não", "né", "ah", "oh", "eh", "bom", "gente", "cara",
    "mundo", "todo", "toda", "todos", "todas", "coisa", "coisas", "momento",
    "momentos", "parte", "partes", "video", "vídeo", "corte", "cortes", "trecho",
    "trechos", "mostrar", "mostra", "mostre", "quero", "queria", "pegar", "pega",
    # EN
    "the", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at", "for",
    "with", "without", "from", "by", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would", "can",
    "could", "should", "this", "that", "these", "those", "i", "you", "he",
    "she", "it", "we", "they", "me", "him", "her", "us", "them", "my", "your",
    "his", "its", "our", "their", "what", "which", "who", "when", "where",
    "why", "how", "not", "no", "yes", "just", "very", "really", "so", "than",
    "then", "there", "here", "about", "into", "over", "after", "before",
    # ES
    "el", "la", "los", "las", "un", "una", "y", "o", "pero", "si", "de", "del",
    "en", "por", "para", "con", "sin", "que", "es", "son", "fue", "ser", "estar",
}

PROMPT_GENERIC_TOKENS = {
    "momento", "momentos", "video", "vídeo", "coisa", "coisas", "fala", "falar",
    "ver", "assistir", "parte", "partes", "pessoa", "pessoas", "algum", "alguma",
    "muito", "muita", "muitos", "muitas", "mais", "menos", "melhor", "melhores",
    "pior", "uteis", "úteis", "util", "útil", "verdade", "facil", "fácil", "como",
    "voce", "você", "aqui", "agora", "hoje", "entao", "então", "tipo", "sobre",
    "para", "pra", "porque", "que", "quando", "onde", "qual", "quais", "todo",
    "toda", "todos", "todas", "ser", "estar", "ter", "fazer", "coisa", "geral",
}

PROMPT_SYNONYMS = {
    "engracado": ["risada", "rindo", "engraçado", "piada", "humor", "hilario", "kkk", "haha", "funny", "joke", "laugh"],
    "humor": ["risada", "rindo", "engraçado", "piada", "hilario", "kkk", "haha", "funny", "joke", "laugh"],
    "divertido": ["risada", "rindo", "engraçado", "piada", "hilario", "funny", "fun"],
    "polemica": ["polemico", "polemica", "briga", "discussao", "discussão", "denuncia", "denúncia", "controversia", "controversy"],
    "polemico": ["polemica", "briga", "discussao", "denuncia", "controversia"],
    "dinheiro": ["lucro", "faturamento", "receita", "riqueza", "investimento", "milhao", "milhão", "money", "profit", "revenue"],
    "financas": ["dinheiro", "lucro", "faturamento", "investimento", "mercado", "money", "finance"],
    "negocios": ["empresa", "lucro", "faturamento", "empreendedor", "business", "company"],
    "historia": ["aconteceu", "quando eu", "experiencia", "experiência", "aprendi", "story", "happened", "experience"],
    "emocionante": ["emocao", "emoção", "chorei", "chorar", "emocionado", "emotional", "cried"],
    "engracadas": ["risada", "engraçado", "piada", "humor", "kkk", "funny", "joke"],
    "engracados": ["risada", "engraçado", "engracada", "piada", "humor", "kkk", "funny", "joke"],
    "improvavel": ["inesperado", "surpresa", "absurdo", "unexpected", "unbelievable"],
    "improvaveis": ["inesperado", "surpresa", "absurdo", "unexpected", "unbelievable"],
    "dica": ["dicas", "como", "passo", "metodo", "método", "aprenda", "tutorial", "tip", "how to"],
    "dicas": ["dica", "como", "passo", "aprenda", "tutorial", "tips"],
    "gol": ["gol", "gols", "marcou", "placar", "jogada", "goal", "scored"],
    "jogada": ["jogada", "gol", "drible", "clutch", "play", "goal"],
    "briga": ["briga", "discussao", "discussão", "confusao", "confusão", "fight", "argument"],
    "fofo": ["fofo", "fofinho", "cute", "adoravel", "adorável"],
}


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9#\s]", " ", text)


def _tokens(text: str) -> List[str]:
    return [t for t in _normalize(text).split() if len(t) >= 3 and t not in STOPWORDS]


def _prompt_tokens(text: str) -> List[str]:
    """Tokens that actually describe content, not filler like "como"."""
    return [t for t in _tokens(text) if t not in PROMPT_GENERIC_TOKENS]


def _repetition_penalty(text: str) -> int:
    """Detects Whisper loops such as "6 mais mais, 6 mais mais, ...".

    Repeated hallucinated segments poison both the ranking and the captions.
    The score is intentionally cheap to compute: token frequency, bigram
    repetition and immediate duplicates are enough to catch ASR loops.
    """
    words = _normalize(text).split()
    if len(words) < 8:
        return 0

    counts = Counter(words)
    unique_ratio = len(counts) / len(words)
    top_ratio = max(counts.values()) / len(words)

    penalty = 0
    if top_ratio >= 0.30:
        penalty += 28
    elif top_ratio >= 0.22:
        penalty += 16
    if unique_ratio < 0.38:
        penalty += 22
    elif unique_ratio < 0.55:
        penalty += 10

    bigrams = list(zip(words, words[1:]))
    if bigrams:
        top_bigram = max(Counter(bigrams).values())
        if top_bigram >= 4:
            penalty += 22
        elif top_bigram >= 3:
            penalty += 12

    immediate = sum(1 for first, second in zip(words, words[1:]) if first == second)
    if immediate >= 4:
        penalty += 16
    elif immediate >= 2:
        penalty += 8
    return min(60, penalty)


OUTRO_MARKERS = (
    "se gostou", "deixa o like", "deixe o like", "se inscreve", "inscreva-se",
    "inscreva", "curte o video", "curte o vídeo", "compartilha",
    "obrigado por assistir", "ate a proxima", "até a próxima", "tchau",
    "falou", "fim do video", "fim do vídeo", "final do episodio",
    "proximo episodio", "semana que vem", "valeu pessoal", "like e se inscreve",
    "subscribe", "thanks for watching",
)

GENERIC_OPENINGS = (
    "com essas dicas", "com isso", "com essa dica", "dessa forma", "desse jeito",
    "assim", "por isso", "então", "entao", "no final", "no fim",
)

DANGLING_WORDS = {
    "que", "porque", "e", "mas", "pra", "para", "de", "do", "da", "dos", "das",
    "no", "na", "nos", "nas", "com", "sem", "se", "quando", "onde", "como",
    "um", "uma", "uns", "umas", "o", "a", "os", "as", "esse", "essa", "isso",
    "este", "esta", "aquele", "aquela", "em", "por", "ao", "aos", "num",
    "numa", "nisso", "nesse", "nessa", "outra", "outro", "the", "and", "but", "to", "of", "in",
    "on", "with", "without",
}

VERB_SUFFIXES = (
    "ar", "er", "ir", "ou", "eu", "iu", "ia", "ava", "iam", "ando", "endo",
    "indo", "aram", "eram", "iram", "asse", "esse", "isse",
)

HASHTAG_EXCLUDE = {
    "deixa", "deixe", "gostou", "like", "inscreve", "inscreva", "canal", "video",
    "vídeo", "parte", "coisa", "coisas", "gente", "cara", "hoje", "agora", "aqui",
    "muito", "muita", "muitos", "muitas", "todo", "toda", "todos", "todas",
    "mundo", "ainda", "também", "tambem", "depois", "antes", "durante", "sobre",
    "primeiro", "segundo", "terceiro", "final", "nesse", "neste", "nessa", "nesta",
    "perto", "longe", "dentro", "fora", "entre", "pela", "pelo", "construa",
    "junte", "ande", "fazer", "fala", "falar", "olha", "veja", "vai", "vem",
    "fica", "ficar", "pega", "pegar", "usa", "usar", "joga", "jogar", "ganha",
    "ganhar", "perde", "perder", "comeca", "começar", "termina", "terminar",
    "outra", "outro", "outras", "outros", "mesma", "mesmo", "pessoal",
    "pessoas", "episodio", "episódio", "serie", "série", "voces", "vocês",
    "tudo", "beleza", "assim", "tambem", "também", "apenas", "somente",
    "nao", "não", "sim", "ah", "oh", "pode", "parece", "joguei", "queria",
    "dizer", "achou", "acho", "coisa", "coisas", "mais", "menos",
}

THEME_TAGS = {
    "dinossauro": ["ark", "sobrevivencia"],
    "ark": ["ark", "sobrevivencia"],
    "sobreviver": ["sobrevivencia"],
    "predador": ["sobrevivencia"],
    "predadores": ["sobrevivencia"],
    "dinheiro": ["financas"],
    "lucro": ["financas"],
    "faturamento": ["financas"],
    "investimento": ["investimentos"],
    "treino": ["fitness"],
    "academia": ["fitness"],
    "jogo": ["games"],
    "jogada": ["games"],
    "gol": ["futebol"],
}

INTRO_MARKERS = (
    "fala pessoal", "fala galera", "ola pessoal", "olá pessoal", "e ai pessoal",
    "e aí pessoal", "tudo beleza", "seja bem-vindo", "seja bem vindo",
    "bem-vindo ao meu canal", "bem vindo ao meu canal", "nesse episodio",
    "neste episodio", "no episodio", "terceiro episodio", "episodio da serie",
    "meu canal", "nesse video", "neste video", "trazendo para voces",
    "vou mostrar para voces", "esta chegando", "está chegando",
)

HOUSEKEEPING_MARKERS = (
    "meu canal", "nesse episodio", "neste episodio", "terceiro episodio",
    "episodio da serie", "se inscreve", "deixa o like", "deixe o like",
    "bem-vindo ao meu canal", "bem vindo ao meu canal", "tudo beleza",
    "vou mostrar para voces", "trazendo para voces",
)


def _context_penalty(
    text: str,
    start: float,
    end: float,
    video_duration: float = 0.0,
) -> Tuple[int, str]:
    """Penalizes intros/outros and channel housekeeping.

    A long video often has excellent speech in its first and last minutes
    (welcome + call to action). Those moments score well on audio and pacing,
    but they are terrible Shorts. The penalty is applied only near the edges
    of the source video, so a genuine story at the end is still allowed.
    """
    normalized = _normalize(text)
    intro = any(_normalize(marker) in normalized for marker in INTRO_MARKERS)
    outro = any(_normalize(marker) in normalized for marker in OUTRO_MARKERS)
    housekeeping = any(_normalize(marker) in normalized for marker in HOUSEKEEPING_MARKERS)

    near_start = video_duration > 0 and start < video_duration * 0.08
    near_end = video_duration > 0 and end > video_duration * 0.92

    penalty = 0
    note = ""
    if intro and near_start:
        penalty += 34
        note = "intro do vídeo"
    elif intro:
        penalty += 8
    if outro and near_end:
        penalty += 34
        note = "encerramento do vídeo" if not note else note
    elif outro:
        penalty += 8
    if housekeeping and (near_start or near_end):
        penalty += 10
    if near_start and not intro:
        penalty += 16
        note = "início do vídeo" if not note else note
    if near_end and not outro:
        penalty += 18
        note = "final do vídeo" if not note else note

    return min(48, penalty), note


def _split_sentences(text: str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(text))
    return [clean_text(sentence) for sentence in sentences if clean_text(sentence)]


def _first_sentence(text: str) -> str:
    parts = _split_sentences(text)
    return parts[0] if parts else clean_text(text)


def _is_outro(sentence: str) -> bool:
    normalized = _normalize(sentence)
    return any(marker in normalized for marker in OUTRO_MARKERS)


def _strip_opening(sentence: str) -> str:
    original = clean_text(sentence)
    stripped = re.sub(
        r"^(então|e aí|eai|olha(?: só)?|olhem|escuta|bom|bem|tipo assim|sabe|"
        r"viu|por isso|só que|so que|mas|aí|ai|daí|dai|ah|oh|e|so|well|"
        r"okay|ok|look|listen)[,\s]+",
        "",
        original,
        flags=re.IGNORECASE,
    ).strip()
    stripped = re.sub(
        r"^(hoje\s+)?(eu\s+)?vou\s+te\s+(mostrar|contar|ensinar|falar)"
        r"(\s+sobre)?[,\s]+",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    stripped = re.sub(
        r"^(nesse|neste|no)\s+v[ií]deo[,\s]+", "", stripped, flags=re.IGNORECASE
    ).strip()
    stripped = re.sub(
        r"^(a verdade é que|o segredo é que|sabe o que|imagina que)[,\s]+",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    if len(stripped) < 12:
        return original
    return stripped.strip(" ,.;:-")


COMMON_VERBS = {
    "é", "são", "está", "estão", "era", "eram", "foi", "tem", "têm",
    "faz", "fazem", "vai", "vão", "vou", "vem", "viu", "pode", "podem",
    "quero", "queria", "lembro", "analisa", "aprende", "funciona",
    "parece", "fica", "pega", "joga", "ganha", "perde", "mata", "morre",
    "disse", "diz",
}

VERB_SUFFIX_HINTS = (
    "ar", "er", "ir", "ou", "eu", "iu", "ia", "ava", "ando", "endo",
    "indo", "aram", "eram", "iram", "amos", "emos", "imos", "ei",
)


def _looks_like_verb(text: str) -> bool:
    for word in _normalize(text).split():
        if word in COMMON_VERBS or word.endswith(VERB_SUFFIX_HINTS):
            return True
    return False


def _sentence_score(sentence: str, prompt: str = "", genre: str = "auto") -> float:
    sentence = clean_text(sentence)
    if not sentence or _is_outro(sentence):
        return -999.0
    score = (
        _hook_score(sentence) * 0.46
        + _text_emotion_score(sentence) * 0.14
        + _keyword_score(sentence, genre)[0] * 0.16
    )
    if prompt:
        prompt_score, _ = _prompt_features(prompt, sentence)
        score += prompt_score * 0.24
    low = sentence.lower()
    if re.match(
        r"^(depois|então|entao|e|mas|aí|ai|daí|dai|por isso|não|nao|sim)\b",
        low,
    ):
        score -= 8
    if len(sentence) < 18:
        score -= 10
    if len(sentence) > 135:
        score -= 6
    if "?" in sentence:
        score += 5
    tip_marker = re.search(
        r"\b(dica|dicas|se faz|funciona\w*|truque|segredo|passo a passo|tutorial|explica\w*)\b",
        low,
    )
    if tip_marker:
        wants_tips = any(
            token in _normalize(prompt)
            for token in ("dica", "tutorial", "passo", "explica")
        )
        score += 24 if wants_tips else 5
    digits = len(re.findall(r"\d", sentence))
    if digits:
        if digits <= 2:
            score += 3
        else:
            score -= min(12, digits * 2)
    score -= _repetition_penalty(sentence) * 0.7
    return score


def _best_sentence(text: str, prompt: str = "", genre: str = "auto") -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return clean_text(text)
    return max(sentences, key=lambda sentence: _sentence_score(sentence, prompt, genre))


def _shorten_hook(sentence: str, max_chars: int = 40) -> str:
    base = _strip_opening(sentence)
    first_clause = re.split(r"[,;:—–]|\s+-\s+", base, maxsplit=1)[0].strip()
    rest = base[len(first_clause):].lstrip(" ,;:—–-").strip()
    normalized_first = _normalize(first_clause)
    if rest and (
        len(first_clause) < 16
        or any(normalized_first.startswith(opening) for opening in GENERIC_OPENINGS)
    ):
        base = rest
    elif len(first_clause) >= 16:
        base = first_clause

    base = base.rstrip(" .,;:!?")
    while True:
        words = base.split()
        if len(words) <= 2 or _normalize(words[-1]) not in DANGLING_WORDS:
            break
        base = " ".join(words[:-1]).rstrip(" .,;:")

    if len(base) > max_chars:
        cut = base[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.;:-")
        base = cut or base[:max_chars]
    return base.strip()


def _trim_dangling(text: str, max_chars: Optional[int] = None) -> str:
    text = (text or "").strip(" \"'“”‘’")
    if max_chars and len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.;:-")
        text = cut or text[:max_chars]
    text = text.rstrip(" .,;:!?")
    while True:
        words = text.split()
        if len(words) <= 2 or _normalize(words[-1]) not in DANGLING_WORDS:
            break
        text = " ".join(words[:-1]).rstrip(" .,;:!?")
    return text


def _best_title_text(
    text: str,
    segment_texts: Optional[Sequence[str]] = None,
    prompt: str = "",
    genre: str = "auto",
) -> str:
    """Chooses a coherent headline.

    Whisper often returns one long unpunctuated sentence for an entire clip, so
    picking "the best sentence" just truncates the beginning mid-thought. When
    segment-level texts are available we also score each segment and adjacent
    pair, which usually gives a much cleaner title.
    """
    text = clean_text(text)
    sentences = _split_sentences(text)
    if len(sentences) > 1:
        return _best_sentence(text, prompt, genre)
    if not segment_texts:
        return _best_sentence(text, prompt, genre)

    cleaned = [clean_text(part) for part in segment_texts if clean_text(part)]
    cleaned = [part for part in cleaned if "..." not in part and len(part.split()) >= 3]
    if not cleaned:
        cleaned = [clean_text(part) for part in segment_texts if clean_text(part)]
    if not cleaned:
        return _best_sentence(text, prompt, genre)

    options: List[str] = []
    for index, part in enumerate(cleaned):
        options.append(part)
        if index + 1 < len(cleaned):
            options.append(f"{part} {cleaned[index + 1]}")

    def quality(candidate: str) -> float:
        score = float(_sentence_score(candidate, prompt, genre))
        words = len(candidate.split())
        if candidate.endswith("..."):
            score -= 18
        if words < 5:
            score -= 24
        elif words < 8:
            score -= 10
        elif words > 28:
            score -= 12
        elif words > 22:
            score -= 6
        if _is_outro(candidate):
            score -= 32
        normalized = _normalize(candidate)
        if any(_normalize(marker) in normalized for marker in INTRO_MARKERS):
            score -= 32
        # prefer segments with more complete clauses
        if re.search(r"\b(porque|pois|assim|para)\b", _normalize(candidate)):
            score += 4
        if len(re.findall(r"\d", candidate)) > 3:
            score -= 12
        if not _looks_like_verb(candidate):
            score -= 10
        score -= _repetition_penalty(candidate) * 0.7
        return score

    valuable = [
        candidate for candidate in options
        if not _is_outro(candidate)
        and not any(_normalize(marker) in _normalize(candidate) for marker in INTRO_MARKERS)
        and len(candidate.split()) >= 6
    ]
    if valuable:
        return max(valuable, key=quality)
    return max(options, key=quality)


def _trim_trailing_clause(text: str) -> str:
    """Drops a short unfinished clause at the end ("..., mas em qualquer jogo")."""
    for marker in (", mas ", " mas ", ", porque ", " porque "):
        index = text.rfind(marker)
        if index < 20:
            continue
        tail = text[index + len(marker):]
        if len(tail.split()) <= 5:
            return text[:index].rstrip(" ,;:-")
    return text


def _dedupe_leading_repeats(text: str) -> str:
    """Removes a duplicated phrase at the start (ASR often emits it twice)."""
    words = text.split()
    changed = True
    while changed and len(words) >= 4:
        changed = False
        for size in (5, 4, 3, 2):
            if len(words) < size * 2:
                continue
            first = [word.lower().strip(",.!?;:") for word in words[:size]]
            second = [word.lower().strip(",.!?;:") for word in words[size:size * 2]]
            if first == second:
                words = words[size:]
                changed = True
                break
    return " ".join(words)


def _title_from_text(
    text: str,
    max_chars: int = 68,
    prompt: str = "",
    genre: str = "auto",
    segment_texts: Optional[Sequence[str]] = None,
) -> str:
    text = clean_text(text)
    if not text:
        return "Momento imperdível"

    best = _best_title_text(text, segment_texts=segment_texts, prompt=prompt, genre=genre)
    title = _trim_trailing_clause(_trim_dangling(
        _dedupe_leading_repeats(_strip_opening(best)),
        max_chars=max_chars,
    ))

    title = title.strip(" \"'“”‘’")
    title = title[0].upper() + title[1:] if title else "Momento imperdível"
    if title[-1] not in "!?.":
        title += "!"
    return title


def _hook_from_text(
    text: str,
    max_chars: int = 40,
    prompt: str = "",
    genre: str = "auto",
    segment_texts: Optional[Sequence[str]] = None,
) -> str:
    best = _best_title_text(text, segment_texts=segment_texts, prompt=prompt, genre=genre)
    base = _dedupe_leading_repeats(_shorten_hook(best, max_chars=max_chars))
    if len(base) < 8:
        base = _title_from_text(
            text,
            max_chars=max_chars,
            prompt=prompt,
            genre=genre,
            segment_texts=segment_texts,
        )
        base = base.rstrip("!?.").strip()
    base = base[:max_chars].rstrip(" ,.;:-")
    return base[0].upper() + base[1:] if base else base


def _keyword_list(text: str, limit: int = 6) -> List[str]:
    counts: Dict[str, int] = {}
    for token in _tokens(text):
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0])))
    return [word for word, _ in ranked[:limit]]


def _hashtag_keywords(text: str, limit: int = 4) -> List[str]:
    counts: Dict[str, int] = {}
    for token in _tokens(text):
        if token in HASHTAG_EXCLUDE:
            continue
        if any(token.endswith(suffix) for suffix in VERB_SUFFIXES):
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0])))
    return [word for word, _ in ranked[:limit]]


def _make_hashtags(text: str, genre: str = "auto", language: Optional[str] = None) -> List[str]:
    preset = GENRE_PRESETS.get(genre, GENRE_PRESETS["auto"])
    normalized = _normalize(text)
    genre_matched = any(
        _concept_in_text(keyword, normalized)
        for keyword in (preset.get("keywords") or [])
    )

    raw_tags: List[str] = []
    # Genre tags only when the clip actually matches that genre; otherwise a
    # "humor" selection would tag a serious survival tip as #funny.
    if genre_matched and genre not in {"auto", "viral"}:
        raw_tags.extend(list(preset.get("hashtags", []))[:2])

    for key, theme_tags in THEME_TAGS.items():
        if _concept_in_text(key, normalized):
            raw_tags.extend(theme_tags)

    raw_tags.extend(_hashtag_keywords(text, 4))

    # Keep acronyms/proper nouns such as ARK even when they are short.
    for acronym in re.findall(r"\b[A-Z]{2,5}\b", text):
        raw_tags.append(acronym.lower())

    tags: List[str] = []
    for tag in raw_tags:
        tag = re.sub(r"[^a-z0-9]", "", _normalize(tag))
        if len(tag) < 3:
            continue
        tag = f"#{tag}"
        if tag not in tags:
            tags.append(tag)
    for generic in ("#shorts", "#viral", "#fyp"):
        if generic not in tags:
            tags.append(generic)
    return tags[:9]


def _make_description(
    text: str,
    max_chars: int = 170,
    prompt: str = "",
    genre: str = "auto",
    segment_texts: Optional[Sequence[str]] = None,
) -> str:
    text = clean_text(text)

    # Prefer segment-level text when Whisper returned one long, unpunctuated
    # sentence. It avoids descriptions made of ASR loops or mid-word cuts.
    if segment_texts:
        cleaned = [clean_text(part) for part in segment_texts if clean_text(part)]
        cleaned = [
            part for part in cleaned
            if "..." not in part
            and len(part.split()) >= 4
            and _repetition_penalty(part) < 8
        ]
        if cleaned:
            best = _best_title_text(
                " ".join(cleaned),
                segment_texts=cleaned,
                prompt=prompt,
                genre=genre,
            )
            description = _dedupe_leading_repeats(_strip_opening(best))
            if len(description.split()) < 10:
                indices = [i for i, part in enumerate(cleaned) if part in best]
                index = indices[0] if indices else 0
                neighbour = ""
                if index + 1 < len(cleaned):
                    neighbour = cleaned[index + 1]
                elif index > 0:
                    neighbour = cleaned[index - 1]
                if neighbour and neighbour not in description:
                    description = f"{description} {neighbour}"
            description = _trim_trailing_clause(description)
            description = _trim_dangling(description, max_chars=max_chars)
            if description:
                description = description[0].upper() + description[1:]
                if description[-1] not in ".!?":
                    description += "."
                return description

    sentences = [sentence for sentence in _split_sentences(text) if not _is_outro(sentence)]
    if not sentences:
        return text[:max_chars]

    best = _best_sentence(" ".join(sentences), prompt=prompt, genre=genre)
    try:
        index = next(i for i, sentence in enumerate(sentences) if sentence == best)
    except StopIteration:
        index = 0

    chosen: List[str] = []
    if index == len(sentences) - 1 and index > 0:
        chosen = [sentences[index - 1], sentences[index]]
    else:
        chosen = sentences[index:index + 2]
    description = " ".join(chosen).strip()
    if len(description) > max_chars:
        description = description[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return description or text[:max_chars]


def _hashtags_value(value: Any) -> List[str]:
    if isinstance(value, str):
        items = re.split(r"[\s,]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return []
    tags: List[str] = []
    for item in items:
        tag = re.sub(r"[^a-zA-Z0-9#]", "", str(item).strip())
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        if len(tag) > 2 and tag not in tags:
            tags.append(tag)
    return tags[:9]


def _concept_in_text(term: str, normalized_text: str) -> bool:
    term = _normalize(term).strip()
    if not term:
        return False
    if term in normalized_text:
        return True
    # Loose PT/EN stemming: "engraçados" -> "engraçad" matches "engraçada".
    if len(term) >= 6:
        for suffix in ("s", "es", "a", "o"):
            if term.endswith(suffix):
                root = term[: -len(suffix)]
                if len(root) >= 5 and root in normalized_text:
                    return True
    return False


def _prompt_features(prompt: str, text: str) -> Tuple[int, List[str]]:
    """Returns a 0-100 prompt match score and the matched concepts."""
    prompt = clean_text(prompt)
    if not prompt:
        return 55, []

    prompt_tokens = _prompt_tokens(prompt)
    if not prompt_tokens:
        # The prompt only had generic words ("como", "verdade", "fácil").
        # Treat it as neutral instead of faking a match in every clip.
        return 55, []

    normalized = _normalize(text)
    matched_tokens: List[str] = []
    matched_terms: List[str] = []

    for token in prompt_tokens:
        terms = [token] + list(PROMPT_SYNONYMS.get(token, []))
        if len(token) > 4 and token.endswith("s"):
            terms.append(token[:-1])
        found = [term for term in terms if _concept_in_text(term, normalized)]
        if found:
            matched_tokens.append(token)
            matched_terms.extend(found)

    if not matched_terms:
        # Prompt may be a whole phrase ("momentos em que ele fala de X").
        phrase_parts = [
            part for part in re.split(r"\W+", _normalize(prompt))
            if len(part) >= 4 and part not in STOPWORDS
            and part not in PROMPT_GENERIC_TOKENS
        ]
        matched_terms = [part for part in phrase_parts if part in normalized][:3]
        matched_tokens = matched_terms[:]

    if not matched_terms:
        return 24, []

    coverage = len(set(matched_tokens)) / max(1, len(set(prompt_tokens)))
    score = 24.0 + min(1.0, coverage) * 60.0 + min(14.0, len(set(matched_terms)) * 3.0)
    return int(round(max(0.0, min(100.0, score)))), sorted(set(matched_terms))[:6]


def _hook_score(text: str) -> int:
    opening = clean_text(text)[:150].lower()
    score = 46.0
    if re.match(r"^(por que|como|qual|o que|quando|onde|quem|voc[eê]|já|why|how|what|when|where|who|did you|have you)", opening):
        score += 30
    elif "?" in opening:
        score += 18
    if re.search(r"\b(nunca|sempre|ningu[ée]m|segredo|verdade|erro|aten[cç][aã]o|cuidado|imagina|descobri|descobriram|acabei de)\b", opening):
        score += 20
    if re.match(r"^(eu|quando eu|aconteceu|deixa eu|olha|escuta|imagina|i |when i)", opening):
        score += 12
    if re.search(r"\d", opening[:90]):
        score += 8
    if "!" in opening:
        score += 5
    if re.search(r"\b(hoje|agora|nesse v[ií]deo|neste v[ií]deo|vou te mostrar|vou te contar)\b", opening):
        score += 6
    return int(round(max(0.0, min(100.0, score))))


def _keyword_score(text: str, genre: str) -> Tuple[int, List[str]]:
    normalized = _normalize(text)
    viral = VIRAL_HOOK_KEYWORDS_PT + VIRAL_HOOK_KEYWORDS_EN
    matched_viral = [kw for kw in viral if _normalize(kw) and _normalize(kw) in normalized]
    genre_words = GENRE_PRESETS.get(genre, {}).get("keywords", []) or []
    matched_genre = [kw for kw in genre_words if _normalize(kw) and _normalize(kw) in normalized]

    # Deduplicate accents/spelling variants.
    matched = sorted(set(matched_viral + matched_genre))
    score = 34.0 + min(38.0, len(matched_viral) * 7.0) + min(28.0, len(matched_genre) * 9.0)
    return int(round(max(0.0, min(100.0, score)))), matched[:8]


def _pacing_score(duration: float, word_count: int) -> int:
    if duration <= 0:
        return 40
    wps = word_count / duration
    if 2.5 <= wps <= 4.1:
        score = 92
    elif 2.0 <= wps < 2.5:
        score = 78
    elif 4.1 < wps <= 5.0:
        score = 74
    elif 1.5 <= wps < 2.0:
        score = 60
    elif 5.0 < wps <= 6.0:
        score = 56
    elif wps < 1.5:
        score = 34
    else:
        score = 44
    return score


def _closure_score(text: str) -> int:
    normalized = _normalize(text)
    score = 58
    if any(_normalize(word) in normalized for word in CONCLUSION_WORDS):
        score += 22
    if text.strip().endswith((".", "!", "?")):
        score += 8
    if len(text.split()) >= 35:
        score += 4
    return int(min(100, score))


def _text_emotion_score(text: str) -> int:
    normalized = _normalize(text)
    emphatic = sum(1 for word in EMOTION_WORDS if _normalize(word) in normalized)
    score = 42.0 + min(34.0, emphatic * 9.0)
    if "!" in text:
        score += 8
    if re.search(r"\b(kkk+|haha+|rsrs+|lol)\b", text.lower()):
        score += 14
    if text.count("!") >= 2:
        score += 5
    return int(round(max(0.0, min(100.0, score))))


def score_segment_detailed(
    text: str,
    duration: float,
    word_count: int,
    signals: Optional[Dict[str, Any]] = None,
    prompt: str = "",
    genre: str = "auto",
    start: float = 0.0,
    end: float = 0.0,
    video_duration: float = 0.0,
) -> Dict[str, Any]:
    """Scores a candidate and returns the score plus an explainable breakdown."""
    text = clean_text(text)
    summary = media_signals.summarize_window(signals, start, end)

    hook = _hook_score(text)
    keyword, matched_keywords = _keyword_score(text, genre)
    pacing = _pacing_score(duration, word_count)
    closure = _closure_score(text)
    emotion_text = _text_emotion_score(text)
    emotion = int(round(max(emotion_text, summary.get("emotion_score", 50)) * 0.68 + min(emotion_text, summary.get("emotion_score", 50)) * 0.32))
    audio = int(summary.get("audio_score", 55))
    visual = int(summary.get("visual_score", 55))
    prompt_score, matched_prompt = _prompt_features(prompt, text)
    silence = float(summary.get("silence_ratio", 0.0))
    retention = int(round(
        pacing * 0.52
        + closure * 0.28
        + max(0.0, 100.0 - silence * 100.0) * 0.20
    ))

    breakdown = {
        "hook": int(hook),
        "keywords": int(keyword),
        "pacing": int(pacing),
        "retention": int(retention),
        "emotion": int(emotion),
        "audio": int(audio),
        "visual": int(visual),
        "prompt": int(prompt_score),
    }

    if _prompt_tokens(prompt):
        weights = {
            "prompt": 0.30, "hook": 0.17, "keywords": 0.12, "emotion": 0.12,
            "pacing": 0.09, "audio": 0.08, "visual": 0.07, "retention": 0.05,
        }
    else:
        weights = {
            "prompt": 0.00, "hook": 0.22, "keywords": 0.18, "emotion": 0.16,
            "pacing": 0.13, "audio": 0.13, "visual": 0.10, "retention": 0.08,
        }

    weighted = sum(breakdown[key] * weight for key, weight in weights.items())
    weighted -= max(0.0, silence - 0.42) * 26.0

    context_penalty, context_note = _context_penalty(text, start, end, video_duration)
    repetition = _repetition_penalty(text)
    words_per_second = word_count / max(0.1, duration)
    if words_per_second < 0.45:
        density_penalty = 18
    elif words_per_second < 0.8:
        density_penalty = 10
    elif words_per_second < 1.0:
        density_penalty = 5
    else:
        density_penalty = 0

    # Map the weighted average into the 35-99 range users expect from a
    # virality score, while preserving relative ordering.
    virality = int(round(max(30.0, min(99.0, 35.0 + weighted * 0.64))))
    if context_penalty:
        virality = max(30, virality - context_penalty)
    if repetition:
        virality = max(30, virality - min(30, repetition))
    if density_penalty:
        virality = max(30, virality - density_penalty)

    high: List[str] = []
    if hook >= 72:
        high.append("gancho forte")
    if _prompt_tokens(prompt) and prompt_score >= 68:
        high.append("combina com o pedido")
    if keyword >= 62:
        high.append("palavras de alto impacto")
    if emotion >= 70:
        high.append("alta emoção")
    if audio >= 70:
        high.append("áudio animado")
    if visual >= 64:
        high.append("cenas dinâmicas")
    if pacing >= 82:
        high.append("ritmo acelerado")
    if closure >= 80:
        high.append("fecho satisfatório")
    if silence > 0.40:
        high.append("pausas longas")

    if high:
        reason = " + ".join(high[:4])
    else:
        reason = "Trecho com fala contínua e boa retenção"
    reason = f"{reason} · {duration:.0f}s"
    if matched_prompt:
        reason = f"Pedido: {', '.join(matched_prompt[:3])} · {reason}"
    elif _prompt_tokens(prompt):
        reason = f"Sem match direto com o pedido · {reason}"
    if context_note and context_penalty >= 12:
        reason = f"{context_note} · {reason}"
    if repetition >= 15:
        reason = f"transcrição repetitiva · {reason}"
    if density_penalty >= 10:
        reason = f"pouca fala ({words_per_second:.1f}/s) · {reason}"

    return {
        "virality_score": virality,
        "score_breakdown": breakdown,
        "weighted_score": round(weighted, 2),
        "context_penalty": context_penalty,
        "repetition_penalty": repetition,
        "speech_density": round(words_per_second, 2),
        "density_penalty": density_penalty,
        "reason": reason,
        "matched_keywords": matched_keywords,
        "matched_prompt": matched_prompt,
        "signals": {k: v for k, v in summary.items() if k != "available"},
    }


def score_segment_heuristics(
    text: str,
    duration: float,
    word_count: int,
    signals: Optional[Dict[str, Any]] = None,
    prompt: str = "",
    genre: str = "auto",
) -> float:
    """Backwards-compatible entry point that returns only the virality score."""
    return float(
        score_segment_detailed(
            text, duration, word_count, signals=signals, prompt=prompt, genre=genre
        )["virality_score"]
    )


def _build_candidates(
    segments: List[Dict[str, Any]],
    min_duration: float,
    max_duration: float,
    signals: Optional[Dict[str, Any]],
    prompt: str,
    genre: str,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    num_segs = len(segments)
    video_duration = max((float(seg.get("end", 0.0)) for seg in segments), default=0.0)
    garbage = [
        _repetition_penalty(str(seg.get("text", ""))) >= 35
        for seg in segments
    ]

    for i in range(num_segs):
        start_time = float(segments[i]["start"])
        accumulated_text: List[str] = []
        accumulated_words = 0

        for j in range(i, num_segs):
            if garbage[j]:
                break
            end_time = float(segments[j]["end"])
            duration = end_time - start_time
            accumulated_text.append(segments[j].get("text", ""))
            words = segments[j].get("words") or []
            accumulated_words += len(words) if words else len(str(segments[j].get("text", "")).split())

            if duration < min_duration:
                continue
            if duration > max_duration:
                break

            text = clean_text(" ".join(accumulated_text))
            if len(text.split()) < 12:
                continue

            scored = score_segment_detailed(
                text,
                duration,
                accumulated_words,
                signals=signals,
                prompt=prompt,
                genre=genre,
                start=start_time,
                end=end_time,
                video_duration=video_duration,
            )
            candidates.append({
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "duration": round(duration, 1),
                "title": _title_from_text(
                    text, prompt=prompt, genre=genre, segment_texts=accumulated_text
                ),
                "hook_title": _hook_from_text(
                    text, prompt=prompt, genre=genre, segment_texts=accumulated_text
                ),
                "hook_text": text[:190] + ("..." if len(text) > 190 else ""),
                "text": text,
                "description": _make_description(
                    text, prompt=prompt, genre=genre, segment_texts=accumulated_text
                ),
                "reason": scored["reason"],
                "virality_score": scored["virality_score"],
                "context_penalty": scored.get("context_penalty", 0),
                "repetition_penalty": scored.get("repetition_penalty", 0),
                "speech_density": scored.get("speech_density", 0),
                "density_penalty": scored.get("density_penalty", 0),
                "score_breakdown": scored["score_breakdown"],
                "matched_keywords": scored["matched_keywords"],
                "matched_prompt": scored["matched_prompt"],
                "signals": scored["signals"],
                "source": "heuristic",
            })

    candidates.sort(key=lambda item: item["virality_score"], reverse=True)
    return candidates


def _select_non_overlapping(
    candidates: Sequence[Dict[str, Any]],
    target_count: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    seen_text: set = set()

    for cand in candidates:
        if len(selected) >= target_count:
            break
        if any(
            not (cand["end_time"] <= sel["start_time"] or cand["start_time"] >= sel["end_time"])
            for sel in selected
        ):
            continue
        fingerprint = _normalize(cand["text"])[:120]
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        selected.append(cand)

    return selected


def find_highlights_heuristic(
    segments: List[Dict[str, Any]],
    target_count: int = 3,
    min_duration: float = 20.0,
    max_duration: float = 60.0,
    signals: Optional[Dict[str, Any]] = None,
    prompt: str = "",
    genre: str = "auto",
) -> List[Dict[str, Any]]:
    """Scans rolling windows and returns the best non-overlapping moments."""
    if not segments:
        return []
    candidates = _build_candidates(segments, min_duration, max_duration, signals, prompt, genre)
    return _select_non_overlapping(candidates, target_count)


# ------------------------------------------------------------------ Ollama
def _ollama_available(timeout: float = 2.0) -> bool:
    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def _parse_json_response(raw: str) -> Any:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        pass

    starts = [pos for pos in (raw.find("["), raw.find("{")) if pos >= 0]
    if not starts:
        return None
    start = min(starts)
    closing = "]" if raw[start] == "[" else "}"
    end = raw.rfind(closing)
    if end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


def _ollama_generate(prompt: str, model_name: str, timeout: float = 120.0) -> Optional[str]:
    try:
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": model_name,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.35, "num_predict": 1200},
            },
            timeout=timeout,
        )
        if response.status_code == 200:
            return response.json().get("response", "")
        print(f"[!] Ollama respondeu HTTP {response.status_code}; usando heurística.")
    except Exception as exc:
        print(f"[!] Ollama não disponível ({exc}); usando heurística.")
    return None


def _candidate_block(candidates: Sequence[Dict[str, Any]], text_limit: int = 210) -> str:
    lines = []
    for idx, cand in enumerate(candidates, start=1):
        excerpt = cand["text"][:text_limit] + ("..." if len(cand["text"]) > text_limit else "")
        lines.append(
            f"{idx}. {cand['start_time']:.1f}s-{cand['end_time']:.1f}s "
            f"({cand['duration']:.0f}s, score {cand['virality_score']}): {excerpt}"
        )
    return "\n".join(lines)


def find_highlights_ollama(
    transcript: str,
    segments: List[Dict[str, Any]],
    model_name: str = "llama3",
    target_count: int = 3,
    min_duration: float = 20.0,
    max_duration: float = 60.0,
    signals: Optional[Dict[str, Any]] = None,
    prompt: str = "",
    genre: str = "auto",
) -> Optional[List[Dict[str, Any]]]:
    """Asks a local LLM to rank/refine heuristic candidates.

    Sending a compact candidate list (instead of the whole transcript) keeps
    long podcasts usable on small local models and guarantees that returned
    timestamps line up with real sentence boundaries.
    """
    if not segments or not _ollama_available():
        return None

    candidates = _build_candidates(segments, min_duration, max_duration, signals, prompt, genre)
    if not candidates:
        return None

    pool = candidates[: max(10, min(20, target_count * 5))]
    genre_label = GENRE_PRESETS.get(genre, {}).get("label", "Automático")
    user_request = prompt.strip() or "os melhores momentos virais, com gancho e conclusão"
    prompt_text = f"""Você é um editor profissional de Shorts/Reels/TikTok, especialista em viralização.
O usuário quer: {user_request}
Gênero: {genre_label}

Abaixo estão candidatos já alinhados a frases reais da transcrição.
Escolha os {target_count} melhores momentos. Prefira:
- gancho forte nos primeiros 3 segundos;
- uma ideia completa, com conclusão;
- emoção, humor, polêmica ou informação útil;
- duração entre {min_duration:.0f}s e {max_duration:.0f}s;
- diversidade (não escolha trechos que se sobrepõem).

Candidatos:
{_candidate_block(pool)}

Responda ESTRITAMENTE em JSON:
{{
  "clips": [
    {{
      "index": 1,
      "title": "Título chamativo em português (máx. 65 caracteres)",
      "hook": "Frase curta de 2 a 5 palavras para aparecer no vídeo",
      "virality_score": 92,
      "reason": "Por que este corte é altamente compartilhável"
    }}
  ]
}}
Use apenas os índices da lista acima. Não invente tempos."""
    raw = _ollama_generate(prompt_text, model_name, timeout=120.0)
    parsed = _parse_json_response(raw or "")
    if isinstance(parsed, dict):
        parsed = parsed.get("clips") or parsed.get("results") or parsed.get("items")
    if not isinstance(parsed, list) or not parsed:
        return None

    selected: List[Dict[str, Any]] = []
    used_indexes: set = set()

    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        cand: Optional[Dict[str, Any]] = None
        try:
            idx_int = int(idx)
            if 1 <= idx_int <= len(pool):
                cand = pool[idx_int - 1]
        except (TypeError, ValueError):
            idx_int = 0

        if cand is None:
            # Fallback: match by returned timestamps when the model ignored indexes.
            try:
                start = float(item.get("start_time"))
                end = float(item.get("end_time"))
            except (TypeError, ValueError):
                start = end = None
            if start is not None and end is not None:
                best_overlap = 0.0
                for option in pool:
                    overlap = max(0.0, min(end, option["end_time"]) - max(start, option["start_time"]))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        cand = option

        if cand is None or id(cand) in used_indexes:
            continue
        if any(
            not (cand["end_time"] <= sel["start_time"] or cand["start_time"] >= sel["end_time"])
            for sel in selected
        ):
            continue

        used_indexes.add(id(cand))
        enriched = dict(cand)
        title = clean_text(str(item.get("title") or ""))[:80]
        hook = clean_text(str(item.get("hook") or item.get("hook_title") or ""))[:44]
        reason = clean_text(str(item.get("reason") or ""))[:220]
        if title:
            enriched["title"] = title
        if hook:
            enriched["hook_title"] = hook
        if reason:
            enriched["reason"] = reason
        try:
            score = int(round(float(item.get("virality_score"))))
            if 0 <= score <= 100:
                enriched["virality_score"] = score
        except (TypeError, ValueError):
            pass
        enriched["source"] = "ollama"
        selected.append(enriched)
        if len(selected) >= target_count:
            break

    if not selected:
        return None

    # Fill missing slots with the best remaining heuristic candidates.
    if len(selected) < target_count:
        for cand in pool:
            if len(selected) >= target_count:
                break
            if id(cand) in used_indexes:
                continue
            if any(
                not (cand["end_time"] <= sel["start_time"] or cand["start_time"] >= sel["end_time"])
                for sel in selected
            ):
                continue
            selected.append(dict(cand))
            used_indexes.add(id(cand))

    return selected[:target_count]


# ------------------------------------------------------------- metadata
def _heuristic_metadata(clip: Dict[str, Any], genre: str, prompt: str) -> Dict[str, Any]:
    text = clip.get("text") or clip.get("hook_text") or ""
    title = clip.get("title") or _title_from_text(text, prompt=prompt, genre=genre)
    hook = clip.get("hook_title") or _hook_from_text(text, prompt=prompt, genre=genre)
    return {
        **clip,
        "title": title[:80],
        "hook_title": hook[:44],
        "description": clip.get("description") or _make_description(
            text, prompt=prompt, genre=genre
        ),
        "hashtags": _make_hashtags(text, genre=genre),
        "cta": "Siga para mais cortes como este!",
    }


def _merge_llm_metadata(clips: List[Dict[str, Any]], parsed: Any) -> List[Dict[str, Any]]:
    if isinstance(parsed, dict):
        parsed = parsed.get("clips") or parsed.get("results") or parsed.get("items")
    if not isinstance(parsed, list):
        return clips

    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index")) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(clips)):
            continue
        clip = clips[idx]
        title = clean_text(str(item.get("title") or ""))[:80]
        hook = clean_text(str(item.get("hook") or item.get("hook_title") or ""))[:44]
        description = clean_text(str(item.get("description") or ""))[:200]
        hashtags = _hashtags_value(item.get("hashtags"))
        if title:
            clip["title"] = title
        if hook:
            clip["hook_title"] = hook
        if description:
            clip["description"] = description
        if hashtags:
            clip["hashtags"] = hashtags
    return clips


def enrich_clips(
    clips: List[Dict[str, Any]],
    prompt: str = "",
    genre: str = "auto",
    language: Optional[str] = None,
    use_ollama: bool = False,
    ollama_model: str = "llama3",
) -> List[Dict[str, Any]]:
    """Adds title/hook/description/hashtags/CTA to every selected clip.

    When Ollama is enabled a single batched call rewrites the copy for all
    clips. Any failure leaves the heuristic copy untouched.
    """
    enriched = [_heuristic_metadata(dict(clip), genre=genre, prompt=prompt) for clip in clips]
    if not enriched or not use_ollama or not _ollama_available():
        return enriched

    lines = []
    for idx, clip in enumerate(enriched, start=1):
        lines.append(
            f"{idx}. Trecho ({clip.get('duration', 0)}s): "
            f"{str(clip.get('text') or clip.get('hook_text') or '')[:500]}"
        )
    request = f"""Você é um social media manager especialista em conteúdo viral.
Gênero: {GENRE_PRESETS.get(genre, {}).get('label', 'Automático')}
Pedido do usuário: {prompt.strip() or 'gera os melhores cortes virais'}

Para cada corte abaixo, crie copy pronta para publicar em TikTok/Reels/Shorts.
Responda ESTRITAMENTE em JSON:
{{
  "clips": [
    {{
      "index": 1,
      "title": "gancho magnético, máx. 65 caracteres",
      "hook": "frase de 2 a 5 palavras para o overlay, máx. 35 caracteres",
      "description": "1 frase envolvente para a legenda, máx. 150 caracteres",
      "hashtags": ["#exemplo", "#shorts"]
    }}
  ]
}}

Cortes:
{chr(10).join(lines)}"""
    raw = _ollama_generate(request, ollama_model, timeout=110.0)
    parsed = _parse_json_response(raw or "")
    return _merge_llm_metadata(enriched, parsed)


# --------------------------------------------------------------- main API
def analyze_video_highlights(
    segments: List[Dict[str, Any]],
    full_text: str,
    target_count: int = 3,
    min_duration: float = 20.0,
    max_duration: float = 60.0,
    use_ollama: bool = True,
    ollama_model: str = "llama3",
    prompt: str = "",
    genre: str = "auto",
    language: Optional[str] = None,
    signals: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Main entry point for clip selection."""
    if use_ollama:
        ollama_clips = find_highlights_ollama(
            full_text,
            segments,
            model_name=ollama_model,
            target_count=target_count,
            min_duration=min_duration,
            max_duration=max_duration,
            signals=signals,
            prompt=prompt,
            genre=genre,
        )
        if ollama_clips:
            return ollama_clips

    return find_highlights_heuristic(
        segments,
        target_count=target_count,
        min_duration=min_duration,
        max_duration=max_duration,
        signals=signals,
        prompt=prompt,
        genre=genre,
    )
