"""
providers.py
================
Реестр AI-провайдеров и построение цепочки автоматического
переключения (fallback) между ними.

(файл обновлён: добавлен провайдер VyceAI — VYCEAI)
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
    },
    {
        "id": "vyceai",
        "label": "VyceAI",
        "env_key": "VYCEAI",
        "base_url": "https://api.vyce.ai/v1",
        # Модели, предоставленные VyceAI (на основе списка, присланного пользователем).
        # Поддерживаются как текстовые, так и визуальные модели.
        "text_models": [
            "gpt-5.6",
            "gpt-5.6-terra",
            "grok-4.6",
        ],
        "vision_models": [
            "deepseek-v4-flash-lr",
            "deepseek-v4-pro",
            "grok-imagine-2",
            "grok-imagine",
        ],
        "audio_models": [],
        # Переменная окружения в Render должна быть задана как VYCEAI
        # (или можете изменить на VYCEAI_API_KEY и подправить окружение).
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
            headers = {
                "HTTP-Referer": "https://github.com/vwxcc/firstbot",
                "X-Title": "Fast Answer Telegram Bot",
            }
        # Для VyceAI и других OpenAI-совместимых провайдеров
        # используем OpenAI-клиент с base_url = provider["base_url"].
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
