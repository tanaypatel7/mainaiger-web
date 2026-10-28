"""Bounded, inert text import of ChatGPT and Claude JSON/ZIP exports.

No member is extracted, no attachment is opened, and imported text is never an
instruction. All ChatGPT branches are retained with their parent identifiers.
Unsupported content is counted in the import warnings instead of silently lost.
"""
import hashlib
import io
import json
import posixpath
import stat
import zipfile
import zlib
from collections import Counter


MAX_UPLOAD = 32 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_RATIO = 200
MAX_CONVERSATIONS = 20000
MAX_MESSAGES = 100000
MAX_TEXT = 24 * 1024 * 1024
MAX_MESSAGE_TEXT = 2 * 1024 * 1024


def _identifier(value, fallback):
    if value is None or value == '':
        return fallback
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('A conversation or message identifier has an invalid type')
    value = str(value)
    if len(value) > 512 or '\x00' in value:
        raise ValueError('A conversation or message identifier is too long or invalid')
    return value


def _title(value):
    if value is None:
        return 'Untitled conversation'
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('A conversation title is invalid or exceeds 2,000 characters')
    return value or 'Untitled conversation'


def _load(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON object keys are not supported')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Nonfinite JSON numbers are not supported')

    try:
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError('The export is not valid UTF-8 JSON') from error
    return value


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get('conversations'), list):
            return value['conversations']
        if 'mapping' in value or 'chat_messages' in value:
            return [value]
    return None


def _provider(row):
    if not isinstance(row, dict):
        return None
    if 'mapping' in row and ('title' in row or 'id' in row or 'conversation_id' in row):
        return 'chatgpt'
    if 'chat_messages' in row and ('uuid' in row or 'name' in row):
        return 'claude'
    return None


def _text(content, warnings):
    """Read only known text fields; do not stringify arbitrary multimodal data."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text') and isinstance(part.get('text'), str):
                parts.append(part['text'])
            else:
                warnings['Non-text content blocks were omitted'] += 1
        return '\n'.join(parts)
    if isinstance(content, dict):
        if isinstance(content.get('parts'), list):
            return _text(content['parts'], warnings)
        if isinstance(content.get('text'), str):
            return content['text']
    warnings['Non-text content blocks were omitted'] += 1
    return ''


def _role(value, warnings):
    roles = {'human': 'user', 'user': 'user', 'assistant': 'assistant',
             'system': 'system', 'developer': 'developer', 'tool': 'tool'}
    if isinstance(value, str) and value in roles:
        return roles[value]
    warnings['Messages with an unknown role were labeled unknown'] += 1
    return 'unknown'


def _conversation(row, provider, warnings):
    # Content hashes make exports with missing provider IDs stable across reimports.
    try:
        fallback = 'content-' + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    except (RecursionError, UnicodeError) as error:
        raise ValueError('A conversation has unsupported nesting or invalid Unicode') from error
    messages = []
    if provider == 'chatgpt':
        mapping = row.get('mapping')
        if not isinstance(mapping, dict):
            raise ValueError('ChatGPT conversation mapping must be an object')
        if len(mapping) > MAX_MESSAGES:
            raise ValueError('The export contains too many messages')
        for node_id, node in mapping.items():
            if not isinstance(node, dict):
                raise ValueError('ChatGPT mapping contains an invalid message node')
            message = node.get('message')
            if message is None:  # A normal ChatGPT export contains an empty root.
                continue
            if not isinstance(message, dict):
                raise ValueError('ChatGPT message must be an object')
            author = message.get('author') or {}
            if not isinstance(author, dict):
                raise ValueError('ChatGPT message author must be an object')
            parent = node.get('parent')
            messages.append({'source_id': _identifier(node_id, fallback),
                             'role': _role(author.get('role'), warnings),
                             'text': _text(message.get('content'), warnings),
                             'parent_id': _identifier(parent, None)})
            if isinstance(message.get('metadata'), dict) and message['metadata'].get('attachments'):
                warnings['Messages with attachments were imported as text only'] += 1
        source_id = _identifier(row.get('id') or row.get('conversation_id'), fallback)
        title = _title(row.get('title'))
    else:
        raw_messages = row.get('chat_messages')
        if not isinstance(raw_messages, list):
            raise ValueError('Claude chat_messages must be a list')
        if len(raw_messages) > MAX_MESSAGES:
            raise ValueError('The export contains too many messages')
        for position, message in enumerate(raw_messages):
            if not isinstance(message, dict):
                raise ValueError('Claude message must be an object')
            # Claude exports can carry both text and structured content. Prefer
            # content when present and nonempty, avoiding duplicate text.
            content = message.get('content')
            text = _text(content, warnings) if content else ''
            if not text and isinstance(message.get('text'), str):
                text = message['text']
            messages.append({'source_id': _identifier(message.get('uuid') or message.get('id'), '%s-message-%d' % (fallback, position)),
                             'role': _role(message.get('sender') or message.get('role'), warnings),
                             'text': text,
                             'parent_id': _identifier(message.get('parent_message_uuid') or message.get('parent_uuid'), None)})
            if message.get('attachments') or message.get('files'):
                warnings['Messages with attachments were imported as text only'] += 1
        source_id = _identifier(row.get('uuid') or row.get('id'), fallback)
        title = _title(row.get('name'))
    unique = {}
    for message in messages:
        if len(message['text'].encode('utf-8')) > MAX_MESSAGE_TEXT:
            raise ValueError('A message exceeds the 2 MiB text limit; split the export')
        previous = unique.get(message['source_id'])
        if previous is not None:
            if previous != message:
                raise ValueError('The export has conflicting messages with the same identifier')
            warnings['Duplicate messages were skipped'] += 1
            continue
        unique[message['source_id']] = message
    result = {'source_id': source_id, 'title': title, 'messages': list(unique.values()), 'provider': provider}
    if provider == 'chatgpt' and row.get('current_node') is not None:
        result['current_node'] = _identifier(row['current_node'], None)
    return result


def _zip_json(data, warnings):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise ValueError('The ZIP archive is invalid or incomplete') from error
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError('The ZIP archive contains too many files')
        expanded = json_size = 0
        names = set()
        candidates = []
        for member in members:
            name = member.filename
            parts = name.replace('\\', '/').split('/')
            if not name or '\x00' in member.orig_filename or name.startswith(('/', '\\')) or '\\' in name or '..' in parts or ':' in parts[0]:
                raise ValueError('The ZIP contains an unsafe file path')
            normalized = posixpath.normpath(name)
            if normalized in names:
                raise ValueError('The ZIP contains duplicate file paths')
            names.add(normalized)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise ValueError('ZIP links and special files are not supported')
            if member.flag_bits & 1:
                raise ValueError('Encrypted ZIP files are not supported')
            if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError('Use a standard stored or deflated ZIP archive')
            expanded += member.file_size
            if expanded > MAX_EXPANDED:
                raise ValueError('The ZIP exceeds the 256 MiB expanded-size limit')
            if member.file_size > 1024 * 1024 and member.file_size > max(1, member.compress_size) * MAX_RATIO:
                raise ValueError('The ZIP has an unsafe compression ratio')
            if member.is_dir():
                continue
            if name.lower().endswith('.json') and not name.startswith('__MACOSX/'):
                json_size += member.file_size
                if json_size > MAX_JSON:
                    raise ValueError('The ZIP exceeds the 64 MiB JSON limit')
                candidates.append(member)
            else:
                warnings['Archive attachments and non-JSON files were not read'] += 1
        if not candidates:
            raise ValueError('No conversation JSON was found in this ZIP')
        for member in candidates:
            try:
                with archive.open(member) as stream:
                    content = stream.read(min(member.file_size, MAX_JSON) + 1)
                if len(content) != member.file_size or len(content) > MAX_JSON:
                    raise ValueError('A ZIP member exceeds its declared size or the JSON limit')
                yield member.filename, _load(content)
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error) as error:
                raise ValueError('A ZIP member is corrupt or unreadable') from error


def _browser_conversation(row, warnings):
    if not isinstance(row, dict) or row.get('provider') not in ('chatgpt-browser', 'claude-browser'):
        raise ValueError('Invalid browser capture provider')
    raw = row.get('messages')
    if not isinstance(raw, list) or len(raw) > MAX_MESSAGES:
        raise ValueError('Invalid captured message list')
    messages = []
    for i, message in enumerate(raw):
        if not isinstance(message, dict) or not isinstance(message.get('text'), str):
            raise ValueError('Invalid captured message')
        if len(message['text'].encode('utf-8')) > MAX_MESSAGE_TEXT:
            raise ValueError('Captured message exceeds the text limit')
        messages.append({'source_id': _identifier(message.get('source_id'), 'message-%d' % i),
                         'role': _role(message.get('role'), warnings), 'text': message['text'],
                         'parent_id': _identifier(message.get('parent_id'), None)})
    warnings['Browser reconstruction may omit hidden history, branches and files'] += 1
    return {'provider': row['provider'], 'source_id': _identifier(row.get('source_id'), None),
            'title': _title(row.get('title')), 'messages': messages}


def parse_archive(data, filename):
    """Return normalized conversations with provenance; never persist or execute.

    Accept UTF-8 JSON exports or standard ZIP archives up to 32 MiB. Duplicate
    conversation IDs with identical normalized content are deduplicated within
    this import; conflicting IDs fail rather than silently replacing evidence.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError('Choose a nonempty chat export')
    if len(data) > MAX_UPLOAD:
        raise ValueError('The upload exceeds 32 MiB; split the export')
    if not isinstance(filename, str) or not filename.lower().endswith(('.zip', '.json')):
        raise ValueError('Choose a ChatGPT or Claude .zip or .json export')
    warnings = Counter()
    documents = _zip_json(data, warnings) if filename.lower().endswith('.zip') else [(filename, _load(data))]
    conversations = {}
    providers = set()
    message_count = text_size = 0
    for name, document in documents:
        rows = _rows(document)
        browser = isinstance(document, dict) and document.get('format') == 'mainaiger.browser-capture.v1'
        if not rows or (not browser and not any(_provider(row) for row in rows)):
            warnings['Unrelated JSON files were not imported'] += 1
            continue
        if len(rows) > MAX_CONVERSATIONS:
            raise ValueError('The export contains too many conversations')
        for row in rows:
            provider = row.get('provider') if browser and isinstance(row, dict) else _provider(row)
            if provider is None:
                raise ValueError('A conversation JSON contains unrecognized records; no data was imported')
            conversation = _browser_conversation(row, warnings) if browser else _conversation(row, provider, warnings)
            key = (provider, conversation['source_id'])
            if key in conversations:
                if conversations[key] != conversation:
                    raise ValueError('The export has conflicting conversations with the same identifier')
                warnings['Duplicate conversations were skipped'] += 1
                continue
            message_count += len(conversation['messages'])
            text_size += sum(len(message['text'].encode('utf-8')) for message in conversation['messages'])
            if message_count > MAX_MESSAGES or text_size > MAX_TEXT or len(conversations) >= MAX_CONVERSATIONS:
                raise ValueError('The export exceeds conversation, message, or 24 MiB text limits; split the export')
            conversations[key] = conversation
            providers.add(provider)
    if not conversations:
        raise ValueError('No recognized ChatGPT or Claude conversations were found')
    return {'provider': next(iter(providers)) if len(providers) == 1 else 'mixed',
            'conversations': list(conversations.values()),
            'warnings': ['%s (%d)' % (message, count) for message, count in sorted(warnings.items())],
            'sha256': hashlib.sha256(data).hexdigest()}
