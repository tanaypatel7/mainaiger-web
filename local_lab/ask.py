"""Memory-first, cost-aware answers: search memory, show what reusing or regenerating costs, answer briefly.

Three stages, each a route and each visible on the Memory page:

1. ``plan``: find notes that match the question (persistent memory only; stable notes first), read the
   house rules (``rule`` notes tagged ``ask``), and estimate two costs — **reuse** (nothing to compute)
   and **regenerate** (tokens, seconds and, if a watts figure is set, watt-hours, from ``costs``) —
   with a recommendation: reuse when a stable note clearly matches, else regenerate. A rule note
   tagged ``auto-reuse`` lets the page act on a reuse recommendation without asking.
2. ``reuse``: the answer is the matching note; no model runs; one journal line says so.
3. ``answer``: the model is asked once, with the chosen notes as context and a fixed brevity rule
   (the reader is competent; fewest words; only what is needed), and the reply is saved as a draft
   ``answer`` note that cites the question, the notes used and the model. What it actually cost is
   measured and shown next to the estimate.
"""
import re

from . import costs, memory, models

BRIEF = ('Answer in the fewest words that fully answer the question. The reader is competent: no preamble, no restating the question, '
         'no closing offers, no hedging beyond one short clause where something is uncertain. Give only what is needed; the reader '
         'will ask for more. Plain text or minimal Markdown; code as a code block.')
MAX_QUESTION = 4000
MAX_NOTE_CHARS = 6000
MAX_CONTEXT_CHARS = 20000
REPLY_TOKENS = 200


class AskError(ValueError):
    pass


def _question(text):
    if not isinstance(text, str) or not text.strip():
        raise AskError('Ask something first')
    if len(text) > MAX_QUESTION:
        raise AskError('Keep the question under %d characters' % MAX_QUESTION)
    return text.strip()


def _words(text):
    return [w for w in re.findall(r'[a-z][a-z0-9_-]{2,}', text.lower()) if w not in ('the', 'and', 'for', 'with', 'what', 'how', 'why', 'does', 'this', 'that', 'can', 'you', 'are')]


def house_rules(state_dir):
    """Rule notes tagged ``ask``: their bodies go into the prompt; a tag ``auto-reuse`` allows acting without asking."""
    try:
        rows = memory.listing(state_dir, tier='notes', kind='rule', tag='ask', limit=10)
    except memory.MemoryError:
        return [], False
    rules, auto = [], False
    for row in rows:
        try:
            note = memory.read(state_dir, row['path'])
        except memory.MemoryError:
            continue
        rules.append({'path': row['path'], 'title': row['title'], 'text': note['body'].strip()[:1500]})
        if 'auto-reuse' in row['tags']:
            auto = True
    return rules, auto


def _choose_model(model, installed):
    if model:
        if not isinstance(model, str) or not models.MODEL_NAME.match(model):
            raise AskError('Choose a model name like llama3.2:3b')
        return model
    try:
        rows = installed()
    except Exception:
        rows = []
    return rows[0]['name'] if rows else None


def plan(state_dir, question, model=None, installed=None):
    installed = installed or models.installed_models
    question = _question(question)
    hits = memory.search(state_dir, question, tier='notes', limit=8)
    hits = [h for h in hits if h['kind'] != 'chat']
    bonus = {'stable': 1.0, 'growing': 0.5}  # relevance first; a stable note wins a near tie
    hits.sort(key=lambda h: -(h['score'] + bonus.get(h['status'], 0.0)))
    words = set(_words(question))
    for h in hits:
        title_words = set(_words(h['title']))
        h['title_overlap'] = round(len(words & title_words) / len(words), 2) if words else 0
    best = hits[0] if hits else None
    strong = bool(best and best['status'] == 'stable' and best['title_overlap'] >= 0.5)
    rules, auto = house_rules(state_dir)
    chosen = _choose_model(model, installed)
    context_chars = len(question) + len(BRIEF) + sum(len(r['text']) for r in rules) + sum(min(h['bytes'], MAX_NOTE_CHARS) for h in hits[:3])
    regenerate = costs.estimate(state_dir, chosen, context_chars, REPLY_TOKENS) if chosen else None
    if strong:
        recommendation, why = 'reuse', 'a stable note matches the question closely'
    elif best and not chosen:
        recommendation, why = 'reuse', 'no local model is running; memory is all there is'
    elif chosen:
        recommendation, why = 'regenerate', ('no note matches closely' if not best else 'the closest note is %s, not stable' % best['status'])
    else:
        recommendation, why = 'none', 'nothing in memory matches and no local model is running'
    return {'question': question, 'hits': hits[:5], 'rules': rules, 'model': chosen,
            'reuse': {'tokens': 0, 'seconds': 0, 'watt_hours': 0.0 if regenerate and regenerate['watts'] else None, 'path': best['path'] if best else None},
            'regenerate': regenerate, 'recommendation': recommendation, 'why': why, 'auto': auto and recommendation == 'reuse',
            'use': [h['path'] for h in hits[:3]]}


def reuse(state_dir, question, path):
    question = _question(question)
    note = memory.read(state_dir, path)
    if note['tier'] != 'notes':
        raise AskError('Answers come from notes, not raw chats')
    return {'question': question, 'note': note, 'cost': {'tokens': 0, 'seconds': 0}}


def answer(state_dir, question, model, use=(), chat=None, save=True):
    chat = chat or models.chat
    question = _question(question)
    if not isinstance(model, str) or not models.MODEL_NAME.match(model):
        raise AskError('Choose a model name like llama3.2:3b')
    notes, total = [], 0
    for path in list(use)[:5]:
        try:
            note = memory.read(state_dir, path)
        except memory.MemoryError:
            continue
        if note['tier'] != 'notes':
            continue
        body = note['body'].strip()[:MAX_NOTE_CHARS]
        if total + len(body) > MAX_CONTEXT_CHARS:
            break
        total += len(body)
        notes.append((note, body))
    rules, _ = house_rules(state_dir)
    system = BRIEF
    if rules:
        system += '\n\nHouse rules from the owner:\n' + '\n'.join('- %s' % r['text'].replace('\n', ' ') for r in rules)
    user = ''
    if notes:
        user += 'Notes from memory (use them where they apply; say "not in memory" for what they do not cover):\n\n'
        user += '\n\n'.join('### %s (%s)\n%s' % (n['title'], n['status'], body) for n, body in notes) + '\n\n'
    user += 'Question: ' + question
    result = chat(model, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], timeout=300)
    out = {'question': question, 'model': model, 'ok': bool(result.get('ok')), 'reply': result.get('reply', '') if result.get('ok') else '',
           'error': None if result.get('ok') else result.get('output', 'The model did not answer'), 'elapsed_ms': int(result.get('elapsed_ms') or 0),
           'tokens': result.get('tokens') if isinstance(result.get('tokens'), dict) else {}, 'used': [n['path'] for n, _ in notes], 'note': None}
    if out['ok']:
        costs.record(state_dir, result)
        watts = costs.load(state_dir)['watts']
        out['watt_hours'] = round(out['elapsed_ms'] / 1000.0 * watts / 3600.0, 4) if watts else None
        if save:
            body = out['reply'].strip() + '\n\n## Question\n\n' + question + '\n'
            if notes:
                body += '\n## Notes used\n\n' + '\n'.join('- [[%s]]' % n['title'] for n, _ in notes) + '\n'
            title = 'Q: ' + re.sub(r'\s+', ' ', question)[:70].rstrip(' ?.')
            out['note'] = memory.create(state_dir, title, body, kind='answer', tags=['ask'], sources=['ask: ' + model] + ['note: ' + n['path'] for n, _ in notes], extra={'model': model})
    return out
