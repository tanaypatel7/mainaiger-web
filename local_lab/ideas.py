"""Ideas: "describe what you want" becomes an idea note that grows as you ask more.

The flow is a reviewed step: the description goes to the local model as one thin-slice question
(a title, a one-line summary, a few tags, a few first tasks, in a fixed shape — ``decide.ask``),
the proposal is shown and editable, similar existing ideas are offered ("add to this one
instead"), and only your confirmation writes the note (``memory.create``) or grows one
(``memory.append``). Without a running model the draft is built from the text itself and says
so, so the box works offline too.
"""
import re

from . import decide, memory, models

MAX_TEXT = 8000
QUESTION = ('Someone described something they want to build or do. Turn it into an idea card: a short title (at most 80 '
            'characters, no trailing period), a one-sentence summary in their own terms, up to 6 lowercase tags, and up to 6 '
            'concrete first tasks (each one line, starting with a verb). Assume they are competent; do not pad.')
SCHEMA = {'type': 'object', 'properties': {
    'title': {'type': 'string', 'maxLength': 80}, 'summary': {'type': 'string', 'maxLength': 300},
    'tags': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6}, 'tasks': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6}},
    'required': ['title', 'summary', 'tags', 'tasks']}


class IdeaError(ValueError):
    pass


def _clean_text(text):
    if not isinstance(text, str) or not text.strip():
        raise IdeaError('Describe what you want first')
    if len(text) > MAX_TEXT:
        raise IdeaError('Keep the description under %d characters' % MAX_TEXT)
    return text.strip()


def fallback(text):
    """A draft with no model: the first line as the title, nothing invented."""
    first = next((l.strip() for l in text.splitlines() if l.strip()), 'Idea')
    title = re.sub(r'\s+', ' ', first)[:80].rstrip(' .')
    return {'title': title or 'Idea', 'summary': '', 'tags': [], 'tasks': []}


def clean_draft(draft):
    """A draft as the page sends it back (edited): checked and normalised."""
    if not isinstance(draft, dict):
        raise IdeaError('The draft must be an object')
    title = re.sub(r'\s+', ' ', str(draft.get('title') or '')).strip().rstrip('.')[:80]
    if not title:
        raise IdeaError('The idea needs a title')
    summary = str(draft.get('summary') or '').strip()[:300]
    tags = draft.get('tags') or []
    tasks = draft.get('tasks') or []
    if not isinstance(tags, list) or not isinstance(tasks, list):
        raise IdeaError('tags and tasks are lists')
    tags = [re.sub(r'[^a-z0-9_-]+', '-', str(t).strip().lower()).strip('-')[:40] for t in tags][:6]
    tags = [t for t in dict.fromkeys(tags) if t]
    tasks = [re.sub(r'\s+', ' ', str(t)).strip()[:200] for t in tasks][:6]
    tasks = [t for t in tasks if t]
    return {'title': title, 'summary': summary, 'tags': tags, 'tasks': tasks}


STOPWORDS = {'want', 'that', 'this', 'with', 'from', 'into', 'also', 'have', 'will', 'what', 'when', 'them', 'they', 'your', 'make', 'like', 'more',
             'some', 'just', 'then', 'than', 'there', 'their', 'about', 'would', 'could', 'should', 'mainaiger', 'thing', 'things', 'need', 'needs'}


def similar(state_dir, text, limit=3):
    """Existing idea notes that share real words with the description (for "add to this one instead"):
    at least two distinct content words of the description, or a third of them, must occur in the note."""
    words = []
    for w in re.findall(r'[A-Za-z][A-Za-z0-9_-]{3,}', text.lower()):
        if w not in STOPWORDS and w not in words:
            words.append(w)
    words = words[:12]
    if not words:
        return []
    try:
        hits = memory.search(state_dir, ' '.join(words), tier='notes', limit=30)
        root = memory._root(state_dir)
    except memory.MemoryError:
        return []
    needed = max(2, len(words) // 3)
    out = []
    for h in hits:
        if h['kind'] != 'idea':
            continue
        try:
            body = (root / h['path']).read_text(encoding='utf-8', errors='replace').lower()
        except OSError:
            continue
        if sum(1 for w in words if w in body) >= needed:
            out.append({'path': h['path'], 'title': h['title'], 'status': h['status'], 'snippet': h.get('snippet', '')})
        if len(out) >= limit:
            break
    return out


def draft(state_dir, text, model=None, ask=decide.ask, installed=models.installed_models):
    """Propose the idea card. Returns {ok, draft, model, elapsed_ms, attempts, error, similar}; never raises for a model problem."""
    text = _clean_text(text)
    out = {'ok': False, 'draft': fallback(text), 'model': None, 'elapsed_ms': 0, 'attempts': 0, 'error': None, 'similar': similar(state_dir, text)}
    chosen = model
    if not chosen:
        try:
            rows = installed()
        except Exception:
            rows = []
        chosen = rows[0]['name'] if rows else None
    if not chosen:
        out['error'] = 'No local model is running; the draft is built from your words (edit it by hand)'
        return out
    if not isinstance(chosen, str) or not models.MODEL_NAME.match(chosen):
        out['error'] = 'Choose a model name like llama3.2:3b'
        return out
    out['model'] = chosen
    result = ask(chosen, QUESTION, text, SCHEMA, attempts=2)
    out['elapsed_ms'] = int(result.get('elapsed_ms') or 0)
    out['attempts'] = int(result.get('attempts') or 0)
    if result.get('ok'):
        try:
            out['draft'] = clean_draft(result['decision'])
            out['ok'] = True
        except IdeaError as error:
            out['error'] = 'The model\'s answer could not be used: %s' % error
    else:
        out['error'] = result.get('error') or 'The model did not answer'
    return out


def body_for(text, card):
    lines = []
    if card['summary']:
        lines += [card['summary'], '']
    lines += ['## What I asked for', '', text.strip(), '']
    if card['tasks']:
        lines += ['## Tasks', ''] + ['- [ ] ' + t for t in card['tasks']] + ['']
    return '\n'.join(lines)


def create(state_dir, text, card, model=None):
    """Write the idea note (status draft). ``card`` is the (possibly edited) draft."""
    text = _clean_text(text)
    card = clean_draft(card)
    return memory.create(state_dir, card['title'], body_for(text, card), kind='idea', tags=card['tags'], sources=['idea-box' + ((': ' + model) if model else '')])


def grow(state_dir, path, text):
    """Add what was asked to an existing idea note as a dated section (draft becomes growing)."""
    text = _clean_text(text)
    note = memory.read(state_dir, path)
    if note['kind'] != 'idea':
        raise IdeaError('That note is not an idea')
    return memory.append(state_dir, path, 'Asked again', text)
