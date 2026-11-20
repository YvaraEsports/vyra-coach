"""Lokale / kostenlose KI-Modelle über eine OpenAI-kompatible Schnittstelle.

Funktioniert mit:
  - Ollama      (https://ollama.com)          base_url: http://localhost:11434/v1
  - LM Studio   (https://lmstudio.ai)         base_url: http://localhost:1234/v1
  - jedem anderen OpenAI-kompatiblen Server (llama.cpp server, vLLM, Jan, OpenRouter …)

Gleiche Schnittstelle wie LLM (ask / chat), damit der Rest von Yvara AI nichts davon merkt.
Online-Recherche läuft hier über eine kostenlose DuckDuckGo-Suche als eigenes Tool.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web (DuckDuckGo) for current information, e.g. patch notes, CDL results, meta, maps. "
                   "Returns titles, links and short snippets.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def to_openai_content(content: list | str) -> list | str:
    """Anthropic-Inhaltsblöcke (Text/Bild) -> OpenAI-Format."""
    if isinstance(content, str):
        return content
    out: list[dict] = []
    for b in content:
        if b.get("type") == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            out.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    return out


def to_openai_tool(t: dict) -> dict:
    return {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                             "parameters": t["input_schema"]}}


def extract_json(text: str) -> Any:
    """JSON robust aus einer Modellantwort holen (kleine Modelle schreiben gern ```json … ``` drumherum)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def parse_ddg(page: str, limit: int = 6) -> list[dict]:
    """Ergebnisse aus der DuckDuckGo-HTML-Seite lesen."""
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                         page, re.S):
        href, title, rest = m.groups()
        q = urllib.parse.urlparse(html.unescape(href))
        url = urllib.parse.parse_qs(q.query).get("uddg", [html.unescape(href)])[0]
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()  # noqa: E731
        results.append({"title": clean(title), "url": url, "snippet": clean(sn.group(1) if sn else "")})
        if len(results) >= limit:
            break
    return results


async def ddg_search(query: str) -> str:
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (vyra-coach)"}) as s:
            async with s.post("https://html.duckduckgo.com/html/", data={"q": query},
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                page = await r.text()
    except Exception as e:
        return f"search failed: {e}"
    res = parse_ddg(page)
    if not res:
        return "no results"
    return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in res)


class ThinkFilter:
    """Entfernt <think>…</think> aus einem Text-Stream (auch wenn die Tags über mehrere Stücke verteilt sind)."""

    def __init__(self) -> None:
        self.in_think = False

    def feed(self, piece: str) -> str:
        out = ""
        while piece:
            if self.in_think:
                end = piece.find("</think>")
                if end < 0:
                    return out
                self.in_think, piece = False, piece[end + 8:]
            else:
                start = piece.find("<think>")
                if start < 0:
                    return out + piece
                out += piece[:start]
                self.in_think, piece = True, piece[start + 7:]
        return out


class LocalLLM:
    """OpenAI-kompatibler Client (Ollama, LM Studio, …)."""

    def __init__(self, cfg) -> None:
        provider = cfg.get_path("llm.provider", "ollama")
        default_url = "http://localhost:11434/v1" if provider == "ollama" else "http://localhost:1234/v1"
        self.base_url = (cfg.get_path("llm.base_url") or default_url).rstrip("/")
        self.model = cfg.get_path("llm.local_model", "gemma3:12b")
        self.vision = bool(cfg.get_path("llm.local_vision", True))
        self.api_key = cfg.get_path("llm.api_key") or "local"
        self.timeout = float(cfg.get_path("llm.local_timeout_s", 300))
        self.num_ctx = int(cfg.get_path("llm.num_ctx", 8192))
        self.provider = provider
        self._warned_vision = False
        self.last_status = 0

    def _strip_images(self, content: list | str) -> list | str:
        if self.vision or isinstance(content, str):
            return content
        if not self._warned_vision and any(b.get("type") == "image" for b in content):
            log.warning("Lokales Modell ohne Bildverständnis (llm.local_vision: false) – Bilder werden weggelassen")
            self._warned_vision = True
        return [b for b in content if b.get("type") != "image"] or "(no content)"

    async def check(self) -> tuple[bool, str]:
        """Läuft Ollama/LM Studio und ist das Modell geladen?"""
        name = "Ollama" if self.provider == "ollama" else "LM Studio / lokaler Server"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"},
                                 timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json(content_type=None)
        except Exception:
            return False, f"{name} läuft nicht ({self.base_url}). Bitte {name} starten."
        ids = [m.get("id", "") for m in (data or {}).get("data", [])]
        base = lambda x: x.split(":")[0] if x.endswith(":latest") else x  # noqa: E731
        if ids and base(self.model) not in {base(i) for i in ids}:
            hint = f"ollama pull {self.model}" if self.provider == "ollama" else "Modell in LM Studio laden"
            return False, f"Modell „{self.model}“ fehlt. Einmal ausführen: {hint}"
        if not ids:
            hint = f" Einmal ausführen: ollama pull {self.model}" if self.provider == "ollama" else ""
            return False, f"In {name} ist noch kein Modell installiert.{hint}"
        return True, f"{name} ({self.model})"

    async def warmup(self, keep_alive: str = "30m") -> bool:
        """Ollama: Modell vorab in den Speicher laden, damit die erste Antwort nicht ~1 Minute dauert."""
        if self.provider != "ollama":
            return False
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{root}/api/generate", json={"model": self.model, "keep_alive": keep_alive,
                                                                      # gleiches Kontextfenster wie bei den Fragen, sonst lädt
                                                                      # Ollama das Modell bei jeder Frage neu (= Sekunden Wartezeit)
                                                                      "options": {"num_ctx": self.num_ctx}},
                                  timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                    await r.read()
                    return r.status < 400
        except Exception as e:
            log.debug("Vorwärmen fehlgeschlagen: %s", e)
            return False

    async def unload(self, model: str | None = None) -> None:
        """Ollama: Modell sofort aus dem Grafikspeicher werfen (keep_alive 0)."""
        if self.provider != "ollama":
            return
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{self._root}/api/generate", json={"model": model or self.model, "keep_alive": 0},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                    await r.read()
        except Exception as e:
            log.debug("Entladen fehlgeschlagen: %s", e)

    # ------------------------------------------------ Ollama native API (schnell, „Nachdenken“ aus)
    @property
    def _root(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    def _native_messages(self, system: str, content: list | str) -> list[dict]:
        content = self._strip_images(content)
        if isinstance(content, str):
            user: dict = {"role": "user", "content": content}
        else:
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            imgs = [b["source"]["data"] for b in content if b.get("type") == "image"]
            user = {"role": "user", "content": text, **({"images": imgs} if imgs else {})}
        return [{"role": "system", "content": system}, user]

    async def _native_stream(self, system: str, content: list | str, *, max_tokens: int, schema: dict | None = None,
                             temperature: float = 0.6):
        """Ollama /api/chat mit think=false: keine versteckte Denkphase -> erstes Wort nach ~0,5 s."""
        body: dict[str, Any] = {
            "model": self.model, "messages": self._native_messages(system, content), "stream": True,
            "keep_alive": "30m",
            "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens, "temperature": temperature},
        }
        if not getattr(self, "_no_think_param", False):
            body["think"] = False
        if schema:
            body["format"] = schema
        for attempt in range(2):
            flt, got, reason = ThinkFilter(), False, None
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(f"{self._root}/api/chat", json=body,
                                      timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                        if r.status >= 400:
                            txt = await r.text()
                            if attempt == 0 and "think" in txt.lower():
                                body.pop("think", None)  # Modell kann kein An/Aus fürs Nachdenken (z. B. gemma3)
                                self._no_think_param = True
                                continue
                            self.last_status = r.status
                            log.error("Ollama Fehler %s: %s", r.status, txt[:300])
                            if r.status == 404:
                                log.error("Modell '%s' fehlt – einmal 'ollama pull %s' ausführen", self.model, self.model)
                            return
                        async for raw in r.content:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except ValueError:
                                continue
                            piece = flt.feed((data.get("message") or {}).get("content") or "")
                            if piece:
                                got = got or bool(piece.strip())
                                yield piece
                            if data.get("done"):
                                reason = data.get("done_reason")
                                break
                if not got:
                    log.info("Ollama lieferte keinen Text (Grund: %s)", reason or "?")
                    if reason == "length" and not getattr(LocalLLM, "_warned_think", False):
                        LocalLLM._warned_think = True
                        log.warning("Das Modell „%s“ denkt vor jeder Antwort lange nach und lässt sich nicht abschalten – "
                                    "dadurch ist Yvara AI langsam. Empfehlung: in der Yvara-AI-App unter Einstellungen → Lokales "
                                    "Modell „gemma3:12b“ eintragen und speichern (Yvara AI lädt es automatisch).", self.model)
                return
            except aiohttp.ClientConnectorError:
                log.error("Ollama nicht erreichbar (%s) – läuft Ollama?", self._root)
                return
            except TimeoutError:
                log.error("Lokales Modell zu langsam (Timeout %ss)", self.timeout)
                return

    async def _native_text(self, system: str, content: list | str, *, max_tokens: int,
                           schema: dict | None = None) -> str:
        parts = [p async for p in self._native_stream(system, content, max_tokens=max_tokens, schema=schema,
                                                       temperature=0.3 if schema else 0.6)]
        return "".join(parts).strip()

    async def ensure_model(self, model: str | None = None) -> bool:
        """Ollama: fehlt das eingestellte Modell, lädt Yvara AI es selbst herunter (wie 'ollama pull')."""
        if self.provider != "ollama":
            return True
        model = model or self.model
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self._root}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    tags = await r.json(content_type=None)
                names = {m.get("name", "") for m in (tags or {}).get("models", [])}
                base = lambda x: x[:-7] if x.endswith(":latest") else x  # noqa: E731
                if base(model) in {base(n) for n in names}:
                    return True
                log.info("Lade KI-Modell „%s“ herunter (einmalig, mehrere GB) …", model)
                last = -10
                async with s.post(f"{self._root}/api/pull", json={"model": model, "stream": True},
                                  timeout=aiohttp.ClientTimeout(total=None)) as r:
                    async for raw in r.content:
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            continue
                        if d.get("error"):
                            log.error("Modell „%s“ konnte nicht geladen werden: %s", model, d["error"])
                            return False
                        if d.get("total") and d.get("completed"):
                            pct = int(100 * d["completed"] / d["total"])
                            if pct >= last + 10:
                                last = pct
                                log.info("KI-Modell „%s“: %d %%", model, pct)
                        if d.get("status") == "success":
                            log.info("KI-Modell „%s“ ist bereit.", model)
                            return True
        except Exception as e:
            log.debug("Modell-Check fehlgeschlagen: %s", e)
        return False

    async def _post(self, body: dict) -> dict | None:
        self.last_status = 0
        if self.provider == "ollama":
            body.setdefault("options", {})["num_ctx"] = self.num_ctx  # Ollama: größeres Kontextfenster
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{self.base_url}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {self.api_key}"},
                                  timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                    if r.status >= 400:
                        self.last_status = r.status
                        txt = await r.text()
                        log.error("Lokales Modell Fehler %s: %s", r.status, txt[:300])
                        if r.status == 404 and "model" in txt.lower():
                            log.error("Modell '%s' fehlt – einmal 'ollama pull %s' ausführen", self.model, self.model)
                        return None
                    return await r.json()
        except aiohttp.ClientConnectorError:
            log.error("Lokales Modell nicht erreichbar (%s) – läuft Ollama/LM Studio?", self.base_url)
        except TimeoutError:
            log.error("Lokales Modell zu langsam (Timeout %ss)", self.timeout)
        except Exception as e:
            log.error("Lokales Modell: %s", e)
        return None

    async def ask(self, system: str, content: list | str, *, deep: bool = False, schema: dict | None = None,
                  max_tokens: int | None = None, effort: str | None = None) -> Any:
        sys_text = system
        if schema:
            sys_text += ("\n\nRespond ONLY with one JSON object matching this JSON schema, no other text:\n"
                         + json.dumps(schema, ensure_ascii=False))
        if self.provider == "ollama":
            text = await self._native_text(sys_text, content, max_tokens=max_tokens or (8000 if deep else 2000),
                                           schema=schema)
            if schema:
                res = extract_json(text)
                if res is None and text:
                    log.error("Ungültiges JSON vom lokalen Modell: %s", text[:200])
                return res
            return text or None
        body: dict[str, Any] = {
            "model": self.model,
            **({} if deep or self.provider != "ollama" else {"reasoning_effort": "none"}),  # schnell: nicht lange „denken“
            "messages": [{"role": "system", "content": sys_text},
                         {"role": "user", "content": to_openai_content(self._strip_images(content))}],
            "max_tokens": max_tokens or (8000 if deep else 2000),
            "temperature": 0.6 if not schema else 0.3,
        }
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        data = await self._post(body)
        if data is None and "reasoning_effort" in body and self.last_status in (400, 422):
            body.pop("reasoning_effort")  # ältere Ollama-Versionen kennen das nicht
            data = await self._post(body)
        if data is None and schema and self.last_status in (400, 422, 500):
            # Manche Server kennen json_schema nicht -> einfacher JSON-Modus
            body["response_format"] = {"type": "json_object"}
            data = await self._post(body)
        if not data:
            return None
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if schema:
            res = extract_json(text)
            if res is None:
                log.error("Ungültiges JSON vom lokalen Modell: %s", text[:200])
            return res
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    async def ask_stream(self, system: str, content: list | str, *, max_tokens: int = 1200):
        """Antwort Stück für Stück (für sofortiges Vorlesen). Gibt Textstücke ohne <think>-Teil zurück."""
        if self.provider == "ollama":
            async for piece in self._native_stream(system, content, max_tokens=max_tokens):
                yield piece
            return
        body: dict[str, Any] = {
            "model": self.model, "stream": True, "max_tokens": max_tokens, "temperature": 0.6,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": to_openai_content(self._strip_images(content))}],
        }
        if self.provider == "ollama":
            body["reasoning_effort"] = "none"
            body.setdefault("options", {})["num_ctx"] = self.num_ctx
        for attempt in range(2):
            in_think, got_text, finish = False, False, None
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(f"{self.base_url}/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {self.api_key}"},
                                      timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                        if r.status >= 400:
                            txt = await r.text()
                            if attempt == 0 and "reasoning_effort" in body and r.status in (400, 422):
                                body.pop("reasoning_effort")
                                continue
                            log.error("Lokales Modell Fehler %s: %s", r.status, txt[:300])
                            return
                        async for raw in r.content:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                if not got_text:
                                    log.info("KI-Stream ohne sichtbaren Text beendet (Grund: %s)", finish or "?")
                                return
                            try:
                                choice = json.loads(data)["choices"][0]
                                delta = choice.get("delta", {})
                                finish = choice.get("finish_reason") or finish
                            except (ValueError, KeyError, IndexError):
                                continue
                            piece = delta.get("content") or ""
                            while piece:
                                if in_think:
                                    end = piece.find("</think>")
                                    if end < 0:
                                        piece = ""
                                    else:
                                        in_think, piece = False, piece[end + 8:]
                                else:
                                    start = piece.find("<think>")
                                    if start < 0:
                                        got_text = got_text or bool(piece.strip())
                                        yield piece
                                        piece = ""
                                    else:
                                        if start:
                                            yield piece[:start]
                                        in_think, piece = True, piece[start + 7:]
                        return
            except aiohttp.ClientConnectorError:
                log.error("Lokales Modell nicht erreichbar (%s) – läuft Ollama/LM Studio?", self.base_url)
                return
            except TimeoutError:
                log.error("Lokales Modell zu langsam (Timeout %ss)", self.timeout)
                return

    async def chat(self, system: str, messages: list[dict], *, web_search: bool = False,
                   client_tools: list[dict] | None = None, tool_handler=None, deep: bool = True,
                   max_steps: int = 6) -> str | None:
        tools = [to_openai_tool(t) for t in (client_tools or [])]
        if web_search:
            tools.append(to_openai_tool(WEB_SEARCH_TOOL))
        msgs: list[dict] = [{"role": "system", "content": system}]
        msgs += [{"role": m["role"], "content": to_openai_content(m["content"])} for m in messages]
        for _ in range(max_steps):
            body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": 4000, "temperature": 0.7}
            if tools:
                body["tools"] = tools
            data = await self._post(body)
            if data is None and tools and self.last_status in (400, 422, 500):
                # Modell kann keine Tools -> ohne Tools nochmal
                log.warning("Modell unterstützt evtl. keine Tools – antworte ohne Recherche/Gedächtnis-Tool")
                tools = []
                continue
            if not data:
                return None
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
                return text or None
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c.get("function", {})
                name = fn.get("name", "")
                try:
                    args = fn.get("arguments") or {}
                    args = json.loads(args) if isinstance(args, str) else args
                    if name == "web_search":
                        out = await ddg_search(str(args.get("query", "")))
                    elif tool_handler:
                        out = await tool_handler(name, args)
                    else:
                        out = "unsupported"
                except Exception as e:
                    out = f"error: {e}"
                msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": str(out)})
        return None
