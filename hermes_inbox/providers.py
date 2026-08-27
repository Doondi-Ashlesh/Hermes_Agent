"""Provider registry.

The classifier is a seam: anything with the signature
`(message, examples, config, client=None) -> Verdict` can serve. Three
implementations ship, which is what proves the seam actually works.

| name        | cost           | learns from corrections | notes                       |
|-------------|----------------|-------------------------|-----------------------------|
| `anthropic` | ~$9-30/month   | yes                     | default; best judgement     |
| `ollama`    | free, local    | yes, less reliably      | needs `ollama serve`        |
| `offline`   | free           | no                      | keyword rules; demos and CI |

`auto` picks `anthropic` when a credential is resolvable, otherwise `offline`,
so a fresh clone runs with no configuration at all.
"""

from __future__ import annotations

NAMES = ("auto", "anthropic", "ollama", "offline")


def resolve(name: str = "auto") -> tuple[object, str]:
    """Return `(classify_fn, resolved_name)`.

    `classify_fn` is None for `anthropic`, meaning "use the Agent's default" —
    it is imported lazily so the SDK is not required to run the other providers.
    """
    name = (name or "auto").lower()
    if name not in NAMES:
        raise ValueError(f"unknown provider {name!r}; expected one of {', '.join(NAMES)}")

    if name == "auto":
        from .offline import has_credentials

        name = "anthropic" if has_credentials() else "offline"

    if name == "anthropic":
        return None, "anthropic"
    if name == "ollama":
        from .ollama import classify as ollama_classify

        return ollama_classify, "ollama"

    from .offline import classify as offline_classify

    return offline_classify, "offline"


def describe(name: str, config) -> str:
    """One line naming what will actually run, for the CLI banner."""
    if name == "anthropic":
        return f"anthropic · {config.model}"
    if name == "ollama":
        return f"ollama · {config.ollama_model} · {config.ollama_host}"
    return "offline keyword rules (does not learn from corrections)"
