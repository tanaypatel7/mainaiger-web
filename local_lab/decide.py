"""Thin-slice decisions: one small question to a model, a JSON answer, checked by code.

The pattern MainAIger uses for every AI-made decision:

1. The *harness* decides what question to ask and what shape the answer must have
   (a small JSON schema: a few fields with types, enums and limits).
2. The *model* answers that one question, in JSON only (Ollama is asked for structured
   output with the schema; the reply is still parsed and checked here, never trusted).
3. The *harness* validates the answer against the schema. If it is wrong, the model is
   told exactly why and asked once more. If it is still wrong, the result is ``ok: False``
   and nothing downstream acts on it.
4. The harness turns the validated object into an action with ordinary code.

So the model never runs anything, never chooses the schema and never gets a second
unbounded try; the harness keeps the budget (``attempts``), the context size and the
decision. Any chat-completion function with the signature of ``models.chat`` can be
plugged in (Ollama today; an OpenAI-compatible endpoint later), which is how the same
decision code runs on a Mac, on Nexus or against ASU Sol.
"""
import json
import re

from . import models as lab_models

MAX_CONTEXT = 24000
MAX_QUESTION = 4000
MAX_SCHEMA_FIELDS = 20
_TYPES = {'string': str, 'number': (int, float), 'integer': int, 'boolean': bool, 'array': list, 'object': dict}


class DecisionError(ValueError):
    pass


# ---------------------------------------------------------------- schema


def check_schema(schema, _nested=False):
    """Accept a small subset of JSON Schema: an object with typed properties, enums, required,
    maxLength, maxItems, items of a simple type or (one level down) a flat object. Returns the normalised schema."""
    if not isinstance(schema, dict) or schema.get('type', 'object') != 'object' or not isinstance(schema.get('properties'), dict):
        raise DecisionError('schema must be {"type": "object", "properties": {...}}')
    props = schema['properties']
    if not 0 < len(props) <= MAX_SCHEMA_FIELDS:
        raise DecisionError('schema needs between 1 and %d properties' % MAX_SCHEMA_FIELDS)
    out = {'type': 'object', 'properties': {}, 'required': []}
    for name, spec in props.items():
        if not isinstance(name, str) or not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,40}$', name) or not isinstance(spec, dict):
            raise DecisionError('property "%s" must have a simple name and an object spec' % name)
        kind = spec.get('type', 'string')
        if kind not in _TYPES:
            raise DecisionError('property "%s": unsupported type %r' % (name, kind))
        clean = {'type': kind}
        if 'enum' in spec:
            if not isinstance(spec['enum'], list) or not spec['enum'] or len(spec['enum']) > 50 or not all(isinstance(v, (str, int, float, bool)) for v in spec['enum']):
                raise DecisionError('property "%s": enum must be a short list of simple values' % name)
            clean['enum'] = list(spec['enum'])
        if 'description' in spec and isinstance(spec['description'], str):
            clean['description'] = spec['description'][:300]
        if 'maxLength' in spec and isinstance(spec['maxLength'], int) and spec['maxLength'] > 0:
            clean['maxLength'] = spec['maxLength']
        if kind == 'array':
            items = spec.get('items', {'type': 'string'})
            if isinstance(items, dict) and items.get('type') == 'object' and not _nested:
                # One level of structure: a list of small objects with simple fields (e.g. memory items to keep).
                clean['items'] = check_schema(items, _nested=True)
            elif not isinstance(items, dict) or items.get('type', 'string') not in ('string', 'number', 'integer', 'boolean'):
                raise DecisionError('property "%s": array items must be a simple type or a flat object' % name)
            else:
                clean['items'] = {'type': items.get('type', 'string')}
            if 'maxItems' in spec and isinstance(spec['maxItems'], int) and spec['maxItems'] > 0:
                clean['maxItems'] = spec['maxItems']
        out['properties'][name] = clean
    required = schema.get('required', list(props))
    if not isinstance(required, list) or not all(r in props for r in required):
        raise DecisionError('required must list property names')
    out['required'] = list(required)
    return out


def validate(value, schema):
    """Return None when value fits the (normalised) schema, else a short reason."""
    if not isinstance(value, dict):
        return 'the answer must be a JSON object'
    extra = set(value) - set(schema['properties'])
    if extra:
        return 'unexpected field(s): ' + ', '.join(sorted(extra))
    for name in schema['required']:
        if name not in value:
            return 'missing field "%s"' % name
    for name, spec in schema['properties'].items():
        if name not in value:
            continue
        v = value[name]
        kind = spec['type']
        if kind == 'integer' and isinstance(v, bool):
            return 'field "%s" must be an integer' % name
        if kind == 'number' and isinstance(v, bool):
            return 'field "%s" must be a number' % name
        if not isinstance(v, _TYPES[kind]):
            return 'field "%s" must be of type %s' % (name, kind)
        if 'enum' in spec and v not in spec['enum']:
            return 'field "%s" must be one of %s' % (name, ', '.join(json.dumps(e) for e in spec['enum']))
        if kind == 'string' and 'maxLength' in spec and len(v) > spec['maxLength']:
            return 'field "%s" is longer than %d characters' % (name, spec['maxLength'])
        if kind == 'array':
            if 'maxItems' in spec and len(v) > spec['maxItems']:
                return 'field "%s" has more than %d items' % (name, spec['maxItems'])
            if spec['items']['type'] == 'object':
                for i, x in enumerate(v):
                    reason = validate(x, spec['items'])
                    if reason:
                        return 'field "%s" item %d: %s' % (name, i + 1, reason)
                continue
            item_type = _TYPES[spec['items']['type']]
            if not all(isinstance(x, item_type) and not (isinstance(x, bool) and spec['items']['type'] in ('integer', 'number')) for x in v):
                return 'field "%s" must contain only %s values' % (name, spec['items']['type'])
    return None


# ---------------------------------------------------------------- parsing


def extract_json(text):
    """The first JSON object in a reply, tolerating code fences and prose around it."""
    if not isinstance(text, str):
        raise DecisionError('empty reply')
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start, end = cleaned.find('{'), cleaned.rfind('}')
    if start == -1 or end <= start:
        raise DecisionError('no JSON object in the reply')
    try:
        return json.loads(cleaned[start:end + 1])
    except ValueError as error:
        raise DecisionError('the JSON could not be parsed: %s' % error)


# ---------------------------------------------------------------- the slice


def ask(model, question, context='', schema=None, chat=lab_models.chat, attempts=2, timeout=300):
    """One decision. Returns {'ok', 'decision', 'raw', 'attempts', 'model', 'elapsed_ms', 'error'}."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        return {'ok': False, 'error': 'The question must be text up to %d characters' % MAX_QUESTION, 'attempts': 0}
    if not isinstance(context, str) or len(context) > MAX_CONTEXT:
        return {'ok': False, 'error': 'The context must be text up to %d characters' % MAX_CONTEXT, 'attempts': 0}
    try:
        schema = check_schema(schema)
    except DecisionError as error:
        return {'ok': False, 'error': str(error), 'attempts': 0}
    attempts = max(1, min(int(attempts), 3))
    system = ('You answer one question with one JSON object and nothing else: no prose, no code fences. '
              'The object must match this JSON schema exactly: ' + json.dumps(schema, separators=(',', ':')))
    user = question.strip() + ('\n\n--- context ---\n' + context if context else '')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    raw, elapsed, used_model = None, 0, model
    for attempt in range(1, attempts + 1):
        result = chat(model, messages, timeout=timeout, format=schema) if _accepts_format(chat) else chat(model, messages, timeout=timeout)
        if not result.get('ok'):
            return {'ok': False, 'error': result.get('output', 'The model did not answer'), 'attempts': attempt, 'model': model, 'raw': raw}
        raw, elapsed, used_model = result.get('reply', ''), elapsed + int(result.get('elapsed_ms') or 0), result.get('model', model)
        try:
            decision = extract_json(raw)
            reason = validate(decision, schema)
        except DecisionError as error:
            decision, reason = None, str(error)
        if reason is None:
            return {'ok': True, 'decision': decision, 'raw': raw, 'attempts': attempt, 'model': used_model, 'elapsed_ms': elapsed, 'error': None}
        messages += [{'role': 'assistant', 'content': raw[:20000]},
                     {'role': 'user', 'content': 'That answer was not acceptable: %s. Reply again with only one JSON object matching the schema.' % reason}]
        last_reason = reason
    return {'ok': False, 'decision': None, 'raw': raw, 'attempts': attempts, 'model': used_model, 'elapsed_ms': elapsed,
            'error': 'No valid answer after %d attempt(s): %s' % (attempts, last_reason)}


def _accepts_format(chat):
    try:
        import inspect
        return 'format' in inspect.signature(chat).parameters
    except (TypeError, ValueError):
        return False
