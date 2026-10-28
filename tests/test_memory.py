"""Owner-review memory, immutable chat provenance and private durable storage."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest

from local_lab.memory import MemoryStore
from local_lab.graph import new_document


def archive(text='original', digest='a', title='Chat'):
    return {'provider': 'chatgpt', 'sha256': hashlib.sha256(digest.encode()).hexdigest(),
            'conversations': [{'source_id': 'chat-1', 'title': title, 'messages': [
                {'source_id': 'm1', 'role': 'user', 'text': text, 'parent_id': None},
                {'source_id': 'm2', 'role': 'assistant', 'text': 'Suggested answer', 'parent_id': 'm1'}]}]}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def add(self, **kwargs):
        return self.store.save_entry(dict(title='Research method', body='Measure the response.', **kwargs))
    def test_import_and_changed_export_are_deduplicated_without_rewriting_originals(self):
        self.assertEqual(self.store.import_chats(archive()), {'duplicate': False, 'conversations': 1, 'messages': 2})
        self.assertTrue(self.store.import_chats(archive())['duplicate'])
        self.assertEqual(self.store.import_chats(archive(digest='b'))['messages'], 0)
        self.assertEqual(self.store.import_chats(archive('new wording', digest='c'))['messages'], 1)
        overview = self.store.overview()
        self.assertEqual(overview['counts']['messages'], 3)
        original = self.store.conversation(overview['conversations'][0]['id'])
        self.assertEqual([m['text'] for m in original['messages']], ['original', 'Suggested answer', 'new wording'])
        self.assertEqual(original['messages'][1]['parent_id'], 'm1')
        self.assertEqual(overview['entries'], [])  # Imported chats are not knowledge.
    def test_bad_import_rolls_back_all_conversations_and_import_digest(self):
        data = archive()
        data['conversations'].append({'source_id': 'chat-2', 'title': 'Invalid', 'messages': [{}]})
        with self.assertRaises(ValueError): self.store.import_chats(data)
        self.assertEqual(self.store.overview()['counts']['conversations'], 0)
        self.assertFalse(self.store.import_chats(archive())['duplicate'])
    def test_creation_is_draft_even_when_client_requests_confirmed(self):
        entry = self.add(status='confirmed', confirmed_at=123)
        self.assertEqual((entry['status'], entry['revision'], entry['confirmed_at']), ('draft', 1, None))
        self.assertEqual(self.store.search('Research'), [])
        entry = self.store.review(entry['id'], 'confirm', 1)
        self.assertEqual(entry['status'], 'confirmed')
        self.assertEqual(self.store.search('Research')[0]['id'], entry['id'])
        self.assertEqual(entry['history'][0]['action'], 'owner.confirm')
    def test_edit_requires_current_revision_and_invalidates_confirmation(self):
        entry = self.add()
        entry = self.store.review(entry['id'], 'confirm', 1)
        with self.assertRaisesRegex(ValueError, 'Reload'):
            self.store.save_entry(dict(entry, expected_revision=1))
        changed = self.store.save_entry(dict(entry, body='New method', expected_revision=2))
        self.assertEqual(changed['status'], 'draft')
        self.assertIsNone(changed['confirmed_at'])
        self.assertEqual(self.store.search('Research'), [])
        self.assertEqual(changed['history'][1]['snapshot']['body'], 'Measure the response.')
        self.assertEqual(changed['history'][0]['revision'], 3)
        with self.assertRaises(ValueError): self.store.review(entry['id'], 'confirm', True)
    def test_review_transitions_and_stale_browser_conflict(self):
        e = self.add()
        e = self.store.review(e['id'], 'reject', 1)
        with self.assertRaises(ValueError): self.store.review(e['id'], 'confirm', 2)
        e = self.store.review(e['id'], 'reopen', 2)
        e = self.store.review(e['id'], 'confirm', 3)
        e = self.store.review(e['id'], 'archive', 4)
        self.assertEqual(e['status'], 'archived')
        self.assertEqual(self.store.search('method'), [])
        with self.assertRaises(ValueError): self.store.review(e['id'], 'reopen', 4)
    def test_ideas_tags_and_sources_are_retained_with_message_provenance(self):
        idea = self.store.create_idea('Research')
        self.store.import_chats(archive())
        c = self.store.overview()['conversations'][0]
        messages = self.store.conversation(c['id'])['messages']
        entry = self.add(idea_id=idea['id'], tags=['workflow', 'workflow'], source_message_ids=[messages[0]['id']])
        self.assertEqual(entry['tags'], ['workflow'])
        self.assertEqual(entry['sources'][0]['text'], 'original')
        self.assertEqual(entry['sources'][0]['provider'], 'chatgpt')
        self.store.review(entry['id'], 'confirm', 1)
        self.assertEqual(len(self.store.search('workflow', idea['id'])), 1)
        self.assertEqual(self.store.search('workflow', 'other-idea'), [])
        with self.assertRaises(ValueError): self.add(source_message_ids=['not-found'])
        with self.assertRaises(ValueError): self.add(idea_id='not-found')
    def test_mainbook_is_validated_stored_and_never_executed(self):
        book = new_document()
        book['notebook']['cells'][0]['source'] = "raise RuntimeError('never execute this')"
        entry = self.add(kind='mainbook', mainbook=book)
        self.assertEqual(entry['mainbook']['notebook']['cells'][0]['source'], book['notebook']['cells'][0]['source'])
        self.assertTrue(self.store.overview()['entries'][0]['has_mainbook'])
        self.assertNotIn('mainbook', self.store.overview()['entries'][0])
        with self.assertRaises(ValueError): self.add(mainbook={'not': 'a mainbook'})
    def test_durable_reopen_preserves_review_and_originals(self):
        self.store.import_chats(archive())
        e = self.add()
        self.store.review(e['id'], 'confirm', 1)
        self.store.close()
        self.store = MemoryStore(self.root)
        self.assertEqual(self.store.entry(e['id'])['status'], 'confirmed')
        self.assertEqual(self.store.overview()['counts']['messages'], 2)
        self.assertEqual(self.store.export()['schema'], 'mainaiger.memory.v1')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
    def test_forget_conversation_preserves_honest_tombstone_and_owned_memories(self):
        self.store.import_chats(archive())
        c = self.store.overview()['conversations'][0]
        mid = self.store.conversation(c['id'])['messages'][0]['id']
        entry = self.add(source_message_ids=[mid])
        self.store.delete_conversation(c['id'])
        self.assertEqual(self.store.overview()['conversations'], [])
        self.assertEqual(self.store.entry(entry['id'])['sources'][0]['text'], '')
        self.assertTrue(self.store.entry(entry['id'])['sources'][0]['deleted'])
        with self.assertRaises(ValueError): self.add(source_message_ids=[mid])
        self.assertEqual(self.store.import_chats(archive(digest='later'))['messages'], 0)
    def test_selected_messages_are_bounded_and_cannot_include_unknown_sources(self):
        self.store.import_chats(archive())
        c = self.store.overview()['conversations'][0]
        mid = self.store.conversation(c['id'])['messages'][0]['id']
        self.assertEqual(self.store.messages([mid, mid])[0]['text'], 'original')
        for ids in ([], 'x', [mid] * 21, ['unknown']):
            with self.assertRaises(ValueError): self.store.messages(ids)
    def test_bad_types_size_and_sql_injection_are_rejected_or_literal(self):
        for fields in ({'tags': 'x'}, {'tags': [{}]}, {'kind': []}, {'source_message_ids': 'x'}, {'body': 'x' * 64001}, {'title': ''}):
            with self.subTest(fields=str(fields)[:30]), self.assertRaises(ValueError):
                self.store.save_entry({'title': 'title', 'body': 'body', **fields})
        entry = self.add()
        self.store.review(entry['id'], 'confirm', 1)
        self.assertEqual(self.store.search("'; DROP TABLE entries; --"), [])
        self.assertEqual(self.store.overview()['counts']['confirmed'], 1)
    def test_search_handles_unicode_case_and_literal_wildcards(self):
        e = self.store.save_entry({'title': 'ÉTUDE résumé', 'body': 'Straße _code'})
        self.store.review(e['id'], 'confirm', 1)
        self.assertEqual(len(self.store.search('étude')), 1)
        self.assertEqual(len(self.store.search('STRASSE')), 1)
        self.assertEqual(self.store.search('_missing'), [])

    def test_archive_over_1000_chats_is_fully_accessible_through_pages(self):
        data = archive()
        data['conversations'] = [{'source_id': 'chat-%d' % i, 'title': 'Chat %d' % i,
                                  'messages': [{'source_id': 'm', 'role': 'user', 'text': 'message %d' % i}]}
                                 for i in range(1005)]
        self.store.import_chats(data)
        seen = []
        page = self.store.overview()
        self.assertEqual((len(page['conversations']), page['limit'], page['offset']), (200, 200, 0))
        while True:
            seen.extend(c['id'] for c in page['conversations'])
            self.assertEqual(page['counts']['conversations'], 1005)
            if page['next_offset'] is None:
                break
            page = self.store.overview(offset=page['next_offset'])
        self.assertEqual(len(seen), 1005)
        self.assertEqual(len(set(seen)), 1005)
        self.assertEqual(len(self.store.conversation(seen[-1])['messages']), 1)
        self.assertEqual(self.store.overview(offset=2000)['conversations'], [])

    def test_pagination_continues_for_longest_list_and_rejects_invalid_bounds(self):
        for i in range(3):
            self.store.create_idea('Idea %d' % i)
        self.add()
        first = self.store.overview(limit=2)
        self.assertEqual(first['next_offset'], 2)
        self.assertEqual(first['counts']['entries'], 1)
        second = self.store.overview(offset=2, limit=2)
        self.assertEqual((len(second['ideas']), second['entries'], second['next_offset']), (1, [], None))
        for kwargs in ({'offset': -1}, {'offset': True}, {'offset': '2'}, {'offset': 10**12},
                       {'limit': 0}, {'limit': 1001}, {'limit': False}, {'limit': 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.overview(**kwargs)

    def test_concurrent_review_cannot_confirm_two_versions(self):
        e = self.add(); outcomes = []
        def review():
            try: self.store.review(e['id'], 'confirm', 1); outcomes.append('ok')
            except ValueError: outcomes.append('conflict')
        threads = [threading.Thread(target=review) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ['ok', 'conflict'])


class PrivateStorageTests(unittest.TestCase):
    def test_symlink_database_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); target = root / 'target'; target.write_text('untouched')
            (root / 'memory.sqlite3').symlink_to(target)
            with self.assertRaises(ValueError): MemoryStore(root)
            self.assertEqual(target.read_text(), 'untouched')
    def test_public_directory_or_database_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); root.chmod(0o755)
            with self.assertRaises(ValueError): MemoryStore(root)
            root.chmod(0o700)
            db = root / 'memory.sqlite3'; db.touch(mode=0o644)
            with self.assertRaises(ValueError): MemoryStore(root)
    def test_symlink_sidecar_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / 'memory.sqlite3-journal').symlink_to(root / 'does-not-exist')
            with self.assertRaises(ValueError): MemoryStore(root)
