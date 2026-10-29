"""Cache markers for multi-turn Anthropic requests, applied to a per-request copy.

Every agentic loop here (the designer, the classifier's evidence reader) re-sends
the tool schemas, the system prompt and the whole conversation on every step.
The first real coding task sent 171 designer requests carrying 30,648,088 input
tokens with not one served from cache (2026-09-27); a pilot turn on 2026-09-28
carried 217,337 input tokens, 0 cached. The parent programme measured the same
shape over 78 environments: ~85% of authoring spend was re-sent prefix, roughly
$157 per environment at the median against $24 with the prefix served from
cache (cache reads cost a tenth of input, a cache write a quarter more; the
markers cost nothing themselves). Verified live 2026-09-28: two marked requests
through the OAuth transport reported `cache_creation_input_tokens 1741` then
`cache_read_input_tokens 1741`.

Two explicit markers, of the four the API allows per request:
  - the system block. Tools render before system, so this one marker caches
    tools and system together, and they are the same bytes for every request
    of every session;
  - the newest cacheable block of the conversation. Each request then reads
    everything before that block from cache and writes only what follows it.
    Earlier breakpoints remain valid read points (the API looks back up to
    twenty positions; a turn adds fewer than five), so hits accrue as the
    conversation grows. A plain-string message (an opening, "Continue.", a
    budget nudge) is left exactly as it is and the marker lands on the block
    before it.

What this must never change is the prompt. `cache_control` is a serving hint,
not content: the model reads byte-identical tools, system and messages at the
same effort, so the output -- and with it the difficulty of a task or the
verdict on a seed -- is unaffected by construction; the tests strip the markers
and check equality. The markers go on a per-request COPY: whatever the caller
stores (`request-config.json`, `conversation.json`, the classifier cache) keeps
its shape and never carries a marker, so nothing accumulates across turns.

Lifetime (2026-10-26): the default entry lives 5 minutes after its last use.
The designer pauses longer than that for controls, pre-reviews and blind
solves, and 597 sessions showed 89% of cache-write tokens were full re-writes
after such a pause -- 48% of designer spend. `ttl="1h"` writes a 1-hour entry
(2x base instead of 1.25x) on both breakpoints; the API requires longer-lived
entries to come first, which two markers of one TTL satisfy. Callers whose
requests follow each other within seconds keep the default.
"""
from __future__ import annotations

CACHE_MARK = {"type": "ephemeral"}
CACHE_TTLS = (None, "5m", "1h")


def cache_mark(ttl: str | None = None) -> dict:
    """The marker for one breakpoint: the 5-minute default, or a `ttl` of "1h"."""
    if ttl not in CACHE_TTLS:
        raise ValueError(f"cache ttl must be one of {CACHE_TTLS[1:]}, not {ttl!r}")
    return {**CACHE_MARK, **({"ttl": ttl} if ttl else {})}

#: Block types the API accepts a marker on. A `thinking` block is not one, and
#: a marker placed on it is a 400 that would end the loop, so the tail marker
#: walks back to the nearest cacheable block and is omitted if there is none.
CACHEABLE_BLOCKS = frozenset({"text", "image", "tool_use", "tool_result", "document"})


def _marked_tail(messages: list, mark: dict | None = None) -> list:
    """`messages` with a marker on the newest cacheable block, or unchanged.

    Walks back from the last message; string-content messages are skipped,
    never rewritten. The returned list, the one message and the one block
    that carry the marker are copies; everything else is shared.
    """
    mark = dict(CACHE_MARK) if mark is None else mark
    for m_index in range(len(messages) - 1, -1, -1):
        message = messages[m_index]
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for b_index in range(len(content) - 1, -1, -1):
            block = content[b_index]
            if isinstance(block, dict) and block.get("type") in CACHEABLE_BLOCKS:
                blocks = list(content)
                blocks[b_index] = {**block, "cache_control": dict(mark)}
                marked = list(messages)
                marked[m_index] = {**message, "content": blocks}
                return marked
    return messages


def with_cache_breakpoints(body: dict, ttl: str | None = None) -> dict:
    """`body` with cache markers on its stable prefix; a new dict, never a mutation.

    The system prompt may be a string or a list of text blocks; anything else
    is refused rather than guessed at, because the system prompt is the
    caller's own contract. Tools are passed through untouched -- they render
    ahead of the system prompt, so its marker already covers them. `ttl`
    ("1h") lengthens both entries' lifetime; None keeps the 5-minute default.
    """
    mark = cache_mark(ttl)
    system = body.get("system")
    if isinstance(system, str) and system:
        cached_system = [{"type": "text", "text": system, "cache_control": dict(mark)}]
    elif (isinstance(system, list) and system
            and all(isinstance(block, dict) and block.get("type") == "text" for block in system)):
        cached_system = [*system[:-1], {**system[-1], "cache_control": dict(mark)}]
    else:
        raise ValueError("cache breakpoints need a text system prompt")
    out = {**body, "system": cached_system}
    if isinstance(body.get("messages"), list):
        out["messages"] = _marked_tail(body["messages"], mark)
    return out
