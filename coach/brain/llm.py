"""Claude-Anbindung (Anthropic SDK): Text- und JSON-Antworten, optional mit Bild."""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def obj(props: dict, required: list[str] | None = None) -> dict:
    """Hilfsfunktion für strikte JSON-Schemas."""
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}


def image_block(b64_jpeg: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_jpeg}}


class LLM:
    def __init__(self, cfg) -> None:
        self.model = cfg.get_path("llm.model", "claude-opus-5-5")
        self.effort_live = cfg.get_path("llm.effort_live", "low")
        self.effort_deep = cfg.get_path("llm.effort_deep", "high")
        self.use_fallbacks = bool(cfg.get_path("llm.use_fallbacks", True))
        self.client = anthropic.AsyncAnthropic(max_retries=2, timeout=120)

    async def check(self) -> tuple[bool, str]:
        """Ist das Gehirn einsatzbereit? (ok, Meldung für die Oberfläche)"""
        import os

        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key or key.startswith("dein-"):
            return False, ("Kein Anthropic-Key in der .env. Entweder Key eintragen oder unter Einstellungen → "
                           "KI-Modell auf Ollama (kostenlos) umstellen.")
        try:
            await self.client.models.retrieve(self.model)
            return True, f"Claude ({self.model})"
        except anthropic.AuthenticationError:
            return False, "Anthropic-Key ungültig – bitte in der .env prüfen."
        except anthropic.APIConnectionError:
            return False, "Keine Internetverbindung zur Anthropic-API."
        except anthropic.APIStatusError as e:
            if e.status_code == 404:
                return True, f"Claude ({self.model})"
            return False, f"Anthropic-API-Fehler {e.status_code}"

    async def _create(self, system: str, content: list | str, effort: str, max_tokens: int, schema: dict | None):
        output_config: dict[str, Any] = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            # Statischer System-Prompt wird gecacht -> günstiger und schneller
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
        )
        if self.use_fallbacks:
            try:
                return await self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as e:
                log.warning("Fallbacks nicht verfügbar (%s) – deaktiviere sie", e.message)
                self.use_fallbacks = False
        return await self.client.messages.create(**kwargs)

    async def ask(
        self,
        system: str,
        content: list | str,
        *,
        deep: bool = False,
        schema: dict | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
    ) -> Any:
        """Gibt Text (ohne schema) bzw. dict (mit schema) zurück, oder None bei Fehler."""
        effort = effort or (self.effort_deep if deep else self.effort_live)
        max_tokens = max_tokens or (16000 if deep else 4000)
        try:
            resp = await self._create(system, content, effort, max_tokens, schema)
        except anthropic.AuthenticationError:
            log.error("Anthropic-API-Key ungültig – ANTHROPIC_API_KEY in .env prüfen")
            return None
        except anthropic.RateLimitError:
            log.warning("Rate-Limit erreicht")
            return None
        except anthropic.APIStatusError as e:
            log.error("API-Fehler %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.error("Keine Verbindung zur Anthropic-API")
            return None

        if resp.stop_reason == "refusal":
            log.warning("Anfrage abgelehnt: %s", getattr(resp, "stop_details", None))
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens":
            log.warning("Antwort abgeschnitten (max_tokens)")
        if schema:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                log.error("Ungültiges JSON von der KI: %s", text[:200])
                return None
        return text

    async def chat(
        self,
        system: str,
        messages: list[dict],
        *,
        web_search: bool = False,
        client_tools: list[dict] | None = None,
        tool_handler=None,
        deep: bool = True,
        max_steps: int = 6,
    ) -> str | None:
        """Mehrstufiger Chat mit optionaler Online-Recherche (web_search) und eigenen Tools.

        tool_handler(name, input) -> str  wird für eigene (client) Tools aufgerufen.
        """
        tools: list[dict] = list(client_tools or [])
        if web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 4})
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": self.effort_deep if deep else self.effort_live},
        )
        if tools:
            kwargs["tools"] = tools
        msgs = list(messages)
        texts: list[str] = []
        try:
            for _ in range(max_steps):
                if self.use_fallbacks:
                    try:
                        resp = await self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", messages=msgs, **kwargs)
                    except anthropic.BadRequestError as e:
                        log.warning("Fallbacks nicht verfügbar (%s) – deaktiviere sie", e.message)
                        self.use_fallbacks = False
                        continue
                else:
                    resp = await self.client.messages.create(messages=msgs, **kwargs)
                if resp.stop_reason == "refusal":
                    return None
                texts += [b.text for b in resp.content if b.type == "text"]
                # Vollständigen Inhalt zurückgeben (inkl. Thinking/Server-Tool-Blöcken)
                msgs.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "pause_turn":
                    continue  # Server-Tool (Websuche) läuft weiter
                if resp.stop_reason != "tool_use":
                    break
                results = []
                for b in resp.content:
                    if b.type == "tool_use":
                        try:
                            out = await tool_handler(b.name, b.input) if tool_handler else "unsupported"
                            results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
                        except Exception as e:
                            results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"error: {e}", "is_error": True})
                msgs.append({"role": "user", "content": results})
                texts = []  # nur der Text nach dem letzten Tool-Schritt ist die Antwort
        except anthropic.AuthenticationError:
            log.error("Anthropic-API-Key ungültig – ANTHROPIC_API_KEY in .env prüfen")
            return None
        except anthropic.APIStatusError as e:
            log.error("API-Fehler %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.error("Keine Verbindung zur Anthropic-API")
            return None
        return "".join(texts).strip() or None


def make_llm(cfg):
    """Wählt das KI-Modell: llm.provider = claude (Anthropic-API, kostet pro Nutzung)
    oder ollama / openai (lokal bzw. OpenAI-kompatibel, z. B. LM Studio – kostenlos)."""
    from ..safety import allowed

    provider = (cfg.get_path("llm.provider") or "ollama").lower()
    if provider == "claude" and not allowed(cfg, "cloud_ai"):
        log.warning("🔒 Privatsphäre: Claude-API ist gesperrt (privacy.allow_cloud_ai: false) – nutze lokale KI (Ollama)")
        cfg.set_path("llm.provider", "ollama")
        provider = "ollama"
    if provider in ("ollama", "openai", "lmstudio", "local"):
        from .local_llm import LocalLLM

        llm = LocalLLM(cfg)
        log.info("KI: lokal über %s (%s)", llm.base_url, llm.model)
        return llm
    return LLM(cfg)
