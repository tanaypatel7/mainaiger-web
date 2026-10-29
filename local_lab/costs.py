"""What an answer costs: measured tokens per second per model, a watts figure you enter, and estimates.

Every chat through this Lab reports its elapsed time and token counts (``models.chat``); ``record``
keeps a running total per model in the private state folder (``model-stats.json``), so the Lab can
say "about 420 tokens, about 25 s, about 0.2 Wh" *before* asking, from what this very device has
measured. Until a model has been measured, the estimate uses stated defaults and says so.

Energy is seconds × a watts figure you enter once per device (``set_watts``); it is an
approximation (the chip's draw while generating), never a measurement, and the UI labels it so.
"""
import json
import os
import time
from pathlib import Path

FILE = 'model-stats.json'
DEFAULT_GEN_TPS = 20.0      # tokens per second generated, a small model on Apple silicon, until measured
DEFAULT_PROMPT_TPS = 250.0  # tokens per second of prompt reading, until measured
CHARS_PER_TOKEN = 4.0
DEFAULT_REPLY_TOKENS = 250


def _path(state_dir):
    return Path(state_dir).resolve(strict=True) / FILE


def load(state_dir):
    path = _path(state_dir)
    if not path.is_file():
        return {'models': {}, 'watts': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'models': {}, 'watts': None}
    if not isinstance(data, dict):
        return {'models': {}, 'watts': None}
    data.setdefault('models', {})
    data.setdefault('watts', None)
    return data


def _save(state_dir, data):
    path = _path(state_dir)
    fd = os.open(path.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(data, out)
    os.replace(path.with_suffix('.tmp'), path)


def record(state_dir, result):
    """Fold one chat result ({'ok', 'model', 'elapsed_ms', 'tokens': {'prompt', 'reply'}}) into the running stats."""
    if not isinstance(result, dict) or not result.get('ok') or not isinstance(result.get('model'), str):
        return None
    tokens = result.get('tokens') if isinstance(result.get('tokens'), dict) else {}
    reply = tokens.get('reply') if isinstance(tokens.get('reply'), int) else None
    prompt = tokens.get('prompt') if isinstance(tokens.get('prompt'), int) else None
    elapsed = max(1, int(result.get('elapsed_ms') or 0))  # a sub-millisecond answer (a fake, a cache) still counts as a call
    if reply is None:
        return None
    data = load(state_dir)
    row = data['models'].setdefault(result['model'], {'calls': 0, 'reply_tokens': 0, 'prompt_tokens': 0, 'elapsed_ms': 0, 'last': None})
    row['calls'] += 1
    row['reply_tokens'] += reply
    row['prompt_tokens'] += prompt or 0
    row['elapsed_ms'] += elapsed
    row['last'] = time.time()
    _save(state_dir, data)
    return row


def set_watts(state_dir, watts):
    if watts is not None and (not isinstance(watts, (int, float)) or isinstance(watts, bool) or not 1 <= watts <= 2000):
        raise ValueError('Enter the watts this device draws while generating (1-2000), or leave it empty')
    data = load(state_dir)
    data['watts'] = float(watts) if watts is not None else None
    _save(state_dir, data)
    return data['watts']


def rates(state_dir, model):
    """(generation tokens/s, prompt tokens/s, 'measured'|'default') for a model on this device."""
    row = load(state_dir)['models'].get(model)
    if row and row.get('reply_tokens', 0) >= 50 and row.get('elapsed_ms', 0) > 0:
        # Prompt reading is usually much faster than generation; without a separate timing the elapsed time is
        # attributed to both by their counts with a fixed ratio, which is honest enough for an estimate.
        total_tokens = row['reply_tokens'] + row['prompt_tokens'] / 10.0
        gen = row['reply_tokens'] / (row['elapsed_ms'] / 1000.0) * (row['reply_tokens'] / total_tokens) if total_tokens else DEFAULT_GEN_TPS
        gen = max(0.5, gen)
        return gen, gen * 10.0, 'measured'
    return DEFAULT_GEN_TPS, DEFAULT_PROMPT_TPS, 'default'


def estimate(state_dir, model, prompt_chars, reply_tokens=DEFAULT_REPLY_TOKENS):
    """Tokens, seconds and (if watts are set) watt-hours for one call, with the basis of each number."""
    gen, prompt_tps, basis = rates(state_dir, model) if model else (DEFAULT_GEN_TPS, DEFAULT_PROMPT_TPS, 'default')
    prompt_tokens = int(prompt_chars / CHARS_PER_TOKEN) + 1
    seconds = prompt_tokens / prompt_tps + reply_tokens / gen
    watts = load(state_dir)['watts']
    return {'model': model, 'prompt_tokens': prompt_tokens, 'reply_tokens': reply_tokens, 'tokens': prompt_tokens + reply_tokens,
            'seconds': round(seconds, 1), 'watt_hours': round(seconds * watts / 3600.0, 4) if watts else None, 'watts': watts,
            'basis': basis, 'tokens_per_second': round(gen, 1)}


def summary(state_dir):
    data = load(state_dir)
    out = {'watts': data['watts'], 'models': {}}
    for name, row in data['models'].items():
        gen, _, basis = rates(state_dir, name)
        out['models'][name] = {'calls': row.get('calls', 0), 'tokens_per_second': round(gen, 1), 'basis': basis, 'reply_tokens': row.get('reply_tokens', 0)}
    return out
