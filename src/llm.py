"""
llm.py — minimal model interface behind which the extraction LLM is swappable.

Phase 4 uses Claude (claude-sonnet-5) at temperature 0. The interface is kept
deliberately thin — `complete()` for raw text, `complete_json()` for a parsed
dict with a re-ask loop on unparseable output — so the same pipeline can run on
a local Ollama model later with zero changes upstream. Anthropic is fully
implemented; the Ollama path mirrors it (native /api/chat) but is untested.

Key handling: `ANTHROPIC_API_KEY` is read from the git-ignored `.env` via a tiny
loader (no python-dotenv dependency). Never commit the key.

Token accounting: every call appends input/output token counts to
`logs/tokens.csv`, keeping the plan's "log tokens even on local runs" habit so
cost never surprises us.
"""
from __future__ import annotations

import csv
import datetime
import json
import re
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# .env loader (no dependency)
# --------------------------------------------------------------------------- #
def load_env(path: Optional[Path] = None) -> None:
    """Populate os.environ from a KEY=VALUE .env file, without overriding a var
    already set in the real environment."""
    import os

    path = path or (REPO_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# JSON extraction from a model reply
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> Optional[dict]:
    """Best-effort parse of a JSON object from a model reply (tolerates ```json
    fences and leading/trailing prose)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
class LLM:
    def __init__(
        self,
        model: str = "claude-sonnet-5",
        temperature: float = 0.0,
        provider: str = "anthropic",
        max_tokens: int = 8192,
        log_path: Optional[Path] = None,
    ):
        self.model = model
        self.temperature = temperature
        self.provider = provider
        self.max_tokens = max_tokens
        self.log_path = log_path or (REPO_ROOT / "logs" / "tokens.csv")
        load_env()
        if provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY
        elif provider != "ollama":
            raise ValueError(f"unknown provider: {provider}")

    # -- raw completion ----------------------------------------------------- #
    def complete(self, system: str, user: str, tag: str = "") -> str:
        if self.provider == "anthropic":
            # Sonnet 5 (and the Opus 4.7+/Fable family) reject `temperature`; the
            # deterministic, no-thinking path is `thinking: disabled` (omitting it
            # would run adaptive thinking, which spends output budget and is
            # non-deterministic). Flip to {"type": "adaptive"} + streaming if the
            # causal-chain quality ever needs deeper reasoning.
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
            self._log(tag, msg.usage.input_tokens, msg.usage.output_tokens)
            return text
        return self._ollama(system, user, tag)

    # -- JSON completion with a re-ask loop --------------------------------- #
    def complete_json(self, system: str, user: str, tag: str = "", retries: int = 2) -> dict:
        prompt = user
        for attempt in range(retries + 1):
            text = self.complete(system, prompt, tag=f"{tag}#{attempt}")
            obj = extract_json(text)
            if obj is not None:
                return obj
            prompt = (
                user
                + "\n\nYour previous reply was not valid JSON. "
                "Return ONLY the JSON object, with no prose or markdown fences."
            )
        raise ValueError(f"{tag}: no valid JSON after {retries + 1} attempts")

    # -- ollama (untested seam) --------------------------------------------- #
    def _ollama(self, system: str, user: str, tag: str) -> str:
        import urllib.request

        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=json.dumps({
                "model": self.model,
                "stream": False,
                "options": {"temperature": self.temperature},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        text = data.get("message", {}).get("content", "")
        self._log(tag, data.get("prompt_eval_count", 0), data.get("eval_count", 0))
        return text

    # -- token log ---------------------------------------------------------- #
    def _log(self, tag: str, inp: int, out: int) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.log_path.exists()
        with self.log_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "provider", "model", "tag", "input_tokens", "output_tokens"])
            w.writerow([
                datetime.datetime.now().isoformat(timespec="seconds"),
                self.provider, self.model, tag, inp, out,
            ])


if __name__ == "__main__":
    # tiny connectivity check (spends a few tokens)
    llm = LLM()
    out = llm.complete("You are a terse assistant.", "Reply with the single word: ok", tag="selftest")
    print("model reply:", out.strip()[:80])
    print("token log ->", llm.log_path)
