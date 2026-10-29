"""Chat import: a ChatGPT or Claude data-export zip becomes chat notes; a review step turns chats into memory.

Import (``inspect_zip`` then ``import_zip``) reads only ``conversations.json`` out of the zip (no other
member is extracted; attachments are listed by name, not copied), recognises the format by shape,
and writes one note per conversation under ``chats/<source>/`` with the conversation's own id in the
front matter (``source_id``) so importing the same export twice adds nothing. Chat notes are never
edited afterwards.

Formats, from the exports as documented and seen in the wild (verify against a real file; the
parsers fail soft, one conversation at a time):

* ChatGPT: a list of conversations, each with ``title``, ``create_time``, ``mapping`` (a tree of
  nodes: ``message.author.role``, ``message.content.parts``, ``parent``, ``children``) and
  ``current_node``. The linear chat is the path from ``current_node`` back to the root.
* Claude: a list of conversations with ``uuid``, ``name``, ``created_at`` and ``chat_messages``
  (``sender`` human|assistant, ``text`` or ``content[].text``, ``attachments``/``files`` by name).

Review (``propose``, ``keep``): one chat at a time, the local model proposes a few memory items in a
fixed shape; the owner keeps, edits or drops them on the Memory page; kept items become draft
notes whose ``sources`` point at the chat note and message numbers; the chat is marked reviewed in
the private review ledger (``memory-review.json``), never in the chat file itself.
"""
import json
import os
import re
import time
import zipfile
from pathlib import Path

from . import decide, memory, models

MAX_ZIP_MEMBER = 400 * 1024 * 1024
MAX_CONVERSATIONS = 5000
MAX_MESSAGE_CHARS = 60000
MAX_CHAT_NOTE = memory.MAX_NOTE - 4096
REVIEW_CONTEXT = 18000
LEDGER = 'memory-review.json'
ITEM_KINDS = ('fact', 'procedure', 'snippet', 'idea', 'rule')
REVIEW_QUESTION = ('Read this conversation and list the things worth keeping as long-term memory for the person who had it: facts about them or '
                   'their work, procedures they settled on, code snippets that worked, ideas they want to pursue, rules they stated for how '
                   'they want things done. Each item: a kind, a short title, the content in their own words (a few lines at most; code as code), '
                   'up to 4 tags, and the message numbers it came from. Skip pleasantries, the assistant\'s boilerplate and anything the person '
                   'rejected. Fewer, better items beat many.')
REVIEW_SCHEMA = {'type': 'object', 'properties': {'items': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'properties': {
    'kind': {'type': 'string', 'enum': list(ITEM_KINDS)}, 'title': {'type': 'string', 'maxLength': 100}, 'text': {'type': 'string', 'maxLength': 2000},
    'tags': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4}, 'messages': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 6}},
    'required': ['kind', 'title', 'text']}}}, 'required': ['items']}


class ImportError_(ValueError):
    pass


# ---------------------------------------------------------------- reading the zip
def _zip_path(candidate):
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 1024:
        raise ImportError_('Enter the path of the export zip')
    path = Path(candidate.strip()).expanduser()
    if not path.is_absolute():
        raise ImportError_('The path must be absolute (start with /)')
    if path.is_symlink() or not path.is_file():
        raise ImportError_('No such file')
    if path.suffix.lower() not in ('.zip', '.json'):
        raise ImportError_('Give the export zip (or its conversations.json)')
    return path


def _conversations_json(path):
    if path.suffix.lower() == '.json':
        if path.stat().st_size > MAX_ZIP_MEMBER:
            raise ImportError_('The file is larger than 400 MB')
        return path.read_text(encoding='utf-8', errors='replace'), None
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            member = next((n for n in names if n.split('/')[-1] == 'conversations.json' and not n.startswith('__MACOSX')), None)
            if member is None:
                raise ImportError_('The zip has no conversations.json; is it a ChatGPT or Claude data export?')
            info = archive.getinfo(member)
            if info.file_size > MAX_ZIP_MEMBER:
                raise ImportError_('conversations.json is larger than 400 MB')
            with archive.open(member) as handle:
                return handle.read(MAX_ZIP_MEMBER + 1).decode('utf-8', 'replace'), [n for n in names if n != member][:50]
    except zipfile.BadZipFile:
        raise ImportError_('That file is not a zip archive')


def detect(rows):
    if not isinstance(rows, list):
        raise ImportError_('conversations.json should hold a list of conversations')
    for row in rows[:20]:
        if isinstance(row, dict):
            if 'mapping' in row:
                return 'chatgpt'
            if 'chat_messages' in row:
                return 'claude'
    raise ImportError_('conversations.json is neither a ChatGPT export (mapping) nor a Claude export (chat_messages)')


def _stamp(value):
    """ISO text from an epoch number or an ISO string; '' when unknown."""
    if isinstance(value, (int, float)) and value > 0:
        return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(value))
    if isinstance(value, str) and value:
        return value[:19].replace(' ', 'T')
    return ''


def _text_of_parts(parts):
    out = []
    for part in parts or []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get('text'), str):
                out.append(part['text'])
            elif part.get('asset_pointer') or part.get('content_type') in ('image_asset_pointer', 'audio_asset_pointer'):
                out.append('[attachment: %s]' % str(part.get('asset_pointer') or part.get('content_type')).split('/')[-1][:80])
    return '\n'.join(t for t in out if t)


def parse_chatgpt(row):
    mapping = row.get('mapping') if isinstance(row.get('mapping'), dict) else {}
    current = row.get('current_node')
    chain = []
    seen = set()
    node = mapping.get(current) if isinstance(current, str) else None
    if node is None:  # no current node: take the longest path from the root instead
        roots = [k for k, v in mapping.items() if isinstance(v, dict) and not v.get('parent')]
        def walk(k):
            v = mapping.get(k) or {}
            kids = [c for c in (v.get('children') or []) if c in mapping]
            best = []
            for c in kids:
                path = walk(c)
                if len(path) > len(best):
                    best = path
            return [k] + best
        chain = walk(roots[0]) if roots else []
    else:
        key = current
        while isinstance(key, str) and key in mapping and key not in seen:
            seen.add(key)
            chain.append(key)
            key = mapping[key].get('parent') if isinstance(mapping[key], dict) else None
        chain.reverse()
    messages = []
    for key in chain:
        msg = (mapping.get(key) or {}).get('message') if isinstance(mapping.get(key), dict) else None
        if not isinstance(msg, dict):
            continue
        role = ((msg.get('author') or {}).get('role') or 'unknown') if isinstance(msg.get('author'), dict) else 'unknown'
        content = msg.get('content') if isinstance(msg.get('content'), dict) else {}
        ctype = content.get('content_type', 'text')
        if ctype == 'text' or ctype == 'multimodal_text':
            text = _text_of_parts(content.get('parts'))
        elif ctype == 'code':
            text = '```%s\n%s\n```' % (str(content.get('language') or ''), str(content.get('text') or ''))
        elif ctype == 'execution_output':
            text = 'Output:\n```\n%s\n```' % str(content.get('text') or '')
        else:
            text = str(content.get('text') or '') or _text_of_parts(content.get('parts'))
        text = text.strip()
        if not text or role == 'system' and not text:
            continue
        if role == 'system' and (msg.get('metadata') or {}).get('is_visually_hidden_from_conversation'):
            continue
        attachments = [str(a.get('name') or a.get('id'))[:120] for a in ((msg.get('metadata') or {}).get('attachments') or []) if isinstance(a, dict)]
        messages.append({'role': {'user': 'user', 'assistant': 'assistant', 'tool': 'tool', 'system': 'system'}.get(role, role), 'text': text[:MAX_MESSAGE_CHARS], 'time': _stamp(msg.get('create_time')), 'attachments': attachments})
    return {'source': 'chatgpt', 'id': str(row.get('conversation_id') or row.get('id') or '')[:80], 'title': str(row.get('title') or 'Untitled')[:120],
            'created': _stamp(row.get('create_time')), 'updated': _stamp(row.get('update_time')), 'messages': messages}


def parse_claude(row):
    messages = []
    for msg in row.get('chat_messages') or []:
        if not isinstance(msg, dict):
            continue
        text = msg.get('text') if isinstance(msg.get('text'), str) else ''
        if not text and isinstance(msg.get('content'), list):
            text = '\n'.join(str(c.get('text')) for c in msg['content'] if isinstance(c, dict) and isinstance(c.get('text'), str))
        attachments = []
        for a in (msg.get('attachments') or []) + (msg.get('files') or []):
            if isinstance(a, dict) and a.get('file_name'):
                attachments.append(str(a['file_name'])[:120])
                if isinstance(a.get('extracted_content'), str) and a['extracted_content'].strip():
                    text += '\n\n[attachment %s]\n%s' % (a['file_name'], a['extracted_content'][:8000])
        text = text.strip()
        if not text:
            continue
        sender = str(msg.get('sender') or 'unknown')
        messages.append({'role': {'human': 'user', 'assistant': 'assistant'}.get(sender, sender), 'text': text[:MAX_MESSAGE_CHARS], 'time': _stamp(msg.get('created_at')), 'attachments': attachments})
    return {'source': 'claude', 'id': str(row.get('uuid') or '')[:80], 'title': str(row.get('name') or 'Untitled')[:120],
            'created': _stamp(row.get('created_at')), 'updated': _stamp(row.get('updated_at')), 'messages': messages}


def parse_all(text):
    try:
        rows = json.loads(text)
    except ValueError as error:
        raise ImportError_('conversations.json could not be parsed: %s' % error)
    source = detect(rows)
    parser = parse_chatgpt if source == 'chatgpt' else parse_claude
    out, errors = [], []
    for i, row in enumerate(rows[:MAX_CONVERSATIONS]):
        if not isinstance(row, dict):
            continue
        try:
            conv = parser(row)
            if conv['messages']:
                out.append(conv)
        except Exception as error:  # one broken conversation must not stop the import
            errors.append('conversation %d: %s' % (i, error))
    return source, out, errors


def inspect_zip(candidate):
    path = _zip_path(candidate)
    text, others = _conversations_json(path)
    source, convs, errors = parse_all(text)
    return {'path': str(path), 'source': source, 'conversations': len(convs), 'messages': sum(len(c['messages']) for c in convs), 'errors': errors[:20],
            'titles': [c['title'] for c in convs[:12]], 'other_files': others or [], 'bytes': path.stat().st_size,
            'first': min((c['created'] for c in convs if c['created']), default=''), 'last': max((c['updated'] or c['created'] for c in convs if c['updated'] or c['created']), default='')}


# ---------------------------------------------------------------- notes
def _existing_ids(state_dir, source):
    root = memory._root(state_dir)
    ids = set()
    for path in memory._files(root, 'chats'):
        if path.parent.name != source:
            continue
        try:
            front, _ = memory.parse(path.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
        if front.get('source_id'):
            ids.add(str(front['source_id']))
    return ids


def chat_body(conv):
    lines = ['Source: %s · %d messages · %s' % (conv['source'], len(conv['messages']), conv['created'] or 'date unknown'), '']
    for n, msg in enumerate(conv['messages'], 1):
        who = {'user': 'You', 'assistant': 'Assistant'}.get(msg['role'], msg['role'].capitalize())
        lines.append('### %d · %s%s' % (n, who, (' · ' + msg['time']) if msg['time'] else ''))
        lines.append('')
        lines.append(msg['text'])
        if msg['attachments']:
            lines.append('')
            lines.append('Attachments: ' + ', '.join(msg['attachments']))
        lines.append('')
    body = '\n'.join(lines)
    if len(body.encode('utf-8')) > MAX_CHAT_NOTE:
        body = body.encode('utf-8')[:MAX_CHAT_NOTE].decode('utf-8', 'ignore') + '\n\n[truncated: the conversation is longer than this note holds]\n'
    return body


def import_zip(state_dir, candidate, limit=None):
    path = _zip_path(candidate)
    text, _ = _conversations_json(path)
    source, convs, errors = parse_all(text)
    known = _existing_ids(state_dir, source)
    imported, skipped = [], 0
    for conv in convs[:limit] if limit else convs:
        if conv['id'] and conv['id'] in known:
            skipped += 1
            continue
        extra = {'source': source, 'source_id': conv['id'], 'messages': len(conv['messages']), 'chat_created': conv['created'], 'chat_updated': conv['updated']}
        try:
            row = memory.create(state_dir, conv['title'], chat_body(conv), kind='chat', tier='chats', subfolder=source, status_='stable', sources=['export: ' + path.name], extra=extra)
        except memory.MemoryError as error:
            errors.append('%s: %s' % (conv['title'], error))
            continue
        known.add(conv['id'])
        imported.append({'path': row['path'], 'title': row['title'], 'messages': len(conv['messages'])})
    return {'source': source, 'imported': len(imported), 'skipped': skipped, 'errors': errors[:50], 'notes': imported[:200], 'file': path.name}


# ---------------------------------------------------------------- review
def _ledger_path(state_dir):
    return Path(state_dir).resolve(strict=True) / LEDGER


def _ledger(state_dir):
    path = _ledger_path(state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(state_dir, data):
    path = _ledger_path(state_dir)
    fd = os.open(path.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(data, out)
    os.replace(path.with_suffix('.tmp'), path)


def review_status(state_dir):
    chats = memory.listing(state_dir, tier='chats', limit=memory.MAX_NOTES)
    ledger = _ledger(state_dir)
    pending = [c for c in chats if c['path'] not in ledger]
    pending.sort(key=lambda c: c['updated'])
    return {'chats': len(chats), 'reviewed': sum(1 for c in chats if c['path'] in ledger), 'pending': len(pending), 'next': pending[0]['path'] if pending else None,
            'next_title': pending[0]['title'] if pending else None, 'kept_total': sum(int(v.get('kept', 0)) for v in ledger.values() if isinstance(v, dict))}


def _chat_context(note):
    body = note['body']
    if len(body) > REVIEW_CONTEXT:
        body = body[:REVIEW_CONTEXT] + '\n\n[the rest of the conversation was cut for length]'
    return 'Title: %s\n\n%s' % (note['title'], body)


def clean_items(items):
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get('kind') if item.get('kind') in ITEM_KINDS else 'fact'
        title = re.sub(r'\s+', ' ', str(item.get('title') or '')).strip()[:100]
        text = str(item.get('text') or '').strip()[:4000]
        if not title or not text:
            continue
        tags = [re.sub(r'[^a-z0-9_-]+', '-', str(t).strip().lower()).strip('-')[:40] for t in (item.get('tags') or []) if str(t).strip()][:4]
        nums = sorted({int(n) for n in (item.get('messages') or []) if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 100000})[:6]
        out.append({'kind': kind, 'title': title, 'text': text, 'tags': [t for t in tags if t], 'messages': nums})
    return out[:12]


def propose(state_dir, chat_path, model=None, ask=None, installed=None):
    """The model's candidate memory items for one chat; nothing is written. Never raises for a model problem."""
    note = memory.read(state_dir, chat_path)
    if note['tier'] != 'chats':
        raise ImportError_('Review works on chat notes')
    ask = ask or decide.ask  # resolved at call time so tests can patch the modules
    installed = installed or models.installed_models
    chosen = model
    if not chosen:
        try:
            rows = installed()
        except Exception:
            rows = []
        chosen = rows[0]['name'] if rows else None
    out = {'ok': False, 'chat': {'path': note['path'], 'title': note['title'], 'messages': note['front'].get('messages')}, 'items': [], 'model': chosen, 'elapsed_ms': 0, 'attempts': 0, 'error': None,
           'truncated': len(note['body']) > REVIEW_CONTEXT}
    if not chosen:
        out['error'] = 'No local model is running; keep items by hand (New note) or start a model on the Models page'
        return out
    if not isinstance(chosen, str) or not models.MODEL_NAME.match(chosen):
        out['error'] = 'Choose a model name like llama3.2:3b'
        return out
    result = ask(chosen, REVIEW_QUESTION, _chat_context(note), REVIEW_SCHEMA, attempts=2)
    out['elapsed_ms'] = int(result.get('elapsed_ms') or 0)
    out['attempts'] = int(result.get('attempts') or 0)
    if result.get('ok'):
        out['items'] = clean_items((result.get('decision') or {}).get('items'))
        out['ok'] = True
    else:
        out['error'] = result.get('error') or 'The model did not answer'
    return out


def keep(state_dir, chat_path, items, model=None):
    """Write the kept (possibly edited) items as draft notes linked to the chat; mark the chat reviewed."""
    note = memory.read(state_dir, chat_path)
    if note['tier'] != 'chats':
        raise ImportError_('Review works on chat notes')
    written = []
    for item in clean_items(items):
        refs = ['chat: %s' % note['path']] + ['chat: %s#%d' % (note['path'], n) for n in item['messages']]
        body = item['text'] + '\n\nFrom [[%s]].\n' % note['title']
        row = memory.create(state_dir, item['title'], body, kind=item['kind'], tags=item['tags'], sources=refs[:8], extra={'reviewed_by': model} if model else None)
        written.append({'path': row['path'], 'title': row['title'], 'kind': row['kind']})
    ledger = _ledger(state_dir)
    ledger[note['path']] = {'at': time.time(), 'kept': len(written), 'model': model}
    _save_ledger(state_dir, ledger)
    return {'chat': note['path'], 'written': written, 'review': review_status(state_dir)}


def skip(state_dir, chat_path):
    note = memory.read(state_dir, chat_path)
    ledger = _ledger(state_dir)
    ledger[note['path']] = {'at': time.time(), 'kept': 0, 'skipped': True}
    _save_ledger(state_dir, ledger)
    return review_status(state_dir)
