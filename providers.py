"""
providers.py
================
Реестр AI-провайдеров и построение цепочки автоматического
переключения (fallback) между ними.

ИДЕЯ:
Почти все современные бесплатные AI-платформы (Groq, Cerebras, Mistral,
SambaNova, OpenRouter и т.д.) предоставляют эндпоинт, совместимый с
OpenAI Chat Completions API. Это значит, что для работы с любой из них
достаточно взять библиотеку `openai` и просто подставить другой
`base_url` + `api_key`. Один и тот же код работает со всеми провайдерами.

КАК ЭТО РАБОТАЕТ:
- Каждый провайдер описан одним словарём в списке PROVIDERS.
- Если переменная окружения провайдера (env_key) не задана —
  провайдер просто пропускается при построении цепочки.
  Бот НЕ падает и не требует наличия всех ключей сразу.
- generate_response()/generate_vision_response() в main.py идут по
  цепочке (provider, model) по очереди, пока одна из моделей не ответит.

КАК ДОБАВИТЬ НОВЫЙ БЕСПЛАТНЫЙ ПРОВАЙДЕР:
1. Найти его OpenAI-совместимый base_url (обычно указан в разделе
   "OpenAI compatibility" / "OpenAI SDK" в документации провайдера).
2. Добавить один словарь в список PROVIDERS ниже.
3. Добавить соответствующую переменную окружения в Render (Dashboard →
   Environment) и в render.yaml (необязательно, но удобно).
Больше ничего менять не нужно — main.py ничего не знает о конкретных
провайдерах, он просто перебирает цепочку.

ВАЖНО ПРО БЕСПЛАТНЫЕ МОДЕЛИ:
Списки бесплатных моделей у OpenRouter, Cerebras и т.п. МЕНЯЮТСЯ
провайдерами без предупреждения (особенно у OpenRouter — там модели
с суффиксом ":free" могут исчезать за несколько дней). Если через
какое-то время конкретная модель начнёт возвращать 404/400 — просто
замените строку в text_models/vision_models на актуальную с сайта
провайдера. Логика переключения от этого не изменится.
"""

import os
from typing import Dict, List, Optional, Tuple

from openai import OpenAI

# ==========================================================
# РЕЕСТР ПРОВАЙДЕРОВ
# ==========================================================
# Порядок в списке = приоритет: первый подключённый провайдер
# пробуется первым, остальные — резерв.

PROVIDERS: List[Dict] = [
    {
        "id": "groq",
        "label": "Groq",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        # Актуально на момент написания (проверено по официальной
        # документации Groq, включая страницу депрекаций моделей).
        "text_models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        "vision_models": ["qwen/qwen3.6-27b", "qwen/qwen3.8-27b"],
        "audio_models": ["whisper-large-v3-turbo", "whisper-large-v3"],
    },
    {
        "id": "cerebras",
        "label": "Cerebras",
        "env_key": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "text_models": ["gpt-oss-120b", "llama-3.3-70b"],
        "vision_models": [],
        "audio_models": [],
        # ВНИМАНИЕ: у Cerebras исторически был бесплатный тариф без
        # привязки карты (1 млн токенов/день), но по недавним данным
        # (август 2026) часть новых аккаунтов уже просит привязать
        # карту для пробных $5. Проверьте актуальные условия на
        # cloud.cerebras.ai перед тем, как полагаться на этот провайдер.
    },
    {
        "id": "sambanova",
        "label": "SambaNova Cloud",
        "env_key": "SAMBANOVA_API_KEY",
        "base_url": "https://api.sambanova.ai/v1",
        "text_models": ["Meta-Llama-3.3-70B-Instruct", "Meta-Llama-3.1-8B-Instruct"],
        "vision_models": [],
        "audio_models": [],
    },
    {
        "id": "mistral",
        "label": "Mistral (La Plateforme)",
        "env_key": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "text_models": ["mistral-small-latest", "open-mistral-7b"],
        "vision_models": ["pixtral-12b-2409"],
        "audio_models": [],
        # Бесплатный тариф не требует карты, но требует подтверждение
        # номера телефона при регистрации на console.mistral.ai.
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        # Модели с суффиксом ":free" МЕНЯЮТСЯ очень часто — сверяйте
        # актуальный список на https://openrouter.ai/models?max_price=0
        "text_models": ["meta-llama/llama-3.3-70b-instruct:free", "z-ai/glm-5.2:free"],
        "vision_models": ["minimax/minimax-m3:free"],
        "audio_models": [],
        # ставим последним в приоритете: бесплатный тариф OpenRouter
        # сильнее всего лимитирован по запросам в день.
    },
]

# Кэш клиентов, чтобы не пересоздавать OpenAI-клиент на каждый вызов
_clients: Dict[str, OpenAI] = {}


def _api_key(provider: Dict) -> Optional[str]:
    return os.getenv(provider["env_key"])


def get_client(provider: Dict) -> OpenAI:
    """Возвращает (и кэширует) OpenAI-совместимый клиент для провайдера."""
    pid = provider["id"]
    if pid not in _clients:
        headers = None
        if pid == "openrouter":
            # OpenRouter просит указывать источник запроса — необязательно,
            # но помогает не попадать под лишние ограничения.
            headers = {
                "HTTP-Referer": "https://github.com/vwxcc/firstbot",
                "X-Title": "Fast Answer Telegram Bot",
            }
        _clients[pid] = OpenAI(
            api_key=_api_key(provider),
            base_url=provider["base_url"],
            default_headers=headers,
        )
    return _clients[pid]


def build_chain(kind: str) -> List[Tuple[Dict, str]]:
    """
    kind: "text_models" | "vision_models" | "audio_models"

    Возвращает плоский список (provider, model_name) в порядке
    приоритета, включающий только провайдеров, у которых в
    переменных окружения задан рабочий API-ключ.
    """
    chain: List[Tuple[Dict, str]] = []
    for provider in PROVIDERS:
        models = provider.get(kind) or []
        if not models:
            continue
        key = _api_key(provider)
        if not key:
            print(f"[AI] {provider['label']}: {provider['env_key']} не задан — пропускаю ({kind})")
            continue
        print(f"[AI] {provider['label']}: подключён, {len(models)} моделей ({kind})")
        for model in models:
            chain.append((provider, model))
    return chain


def describe_missing(kind: str) -> List[str]:
    """Список env-переменных провайдеров, которые ещё можно подключить для данного kind."""
    missing = []
    for provider in PROVIDERS:
        if not provider.get(kind):
            continue
        if not _api_key(provider):
            missing.append(f"{provider['label']} ({provider['env_key']})")
    return missing
