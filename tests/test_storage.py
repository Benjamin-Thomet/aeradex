"""Persistence failures, ambiguous input and agent retry contracts."""
from decimal import Decimal
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from allkvitt import api, check, storage
from allkvitt.book import Book, BookError
from allkvitt.files import (FormatError, MdTable, atomic_write, parse_table, read_yaml,
                           read_frontmatter, render_table, write_yaml)
from allkvitt.testing import make_book


def test_decimal_yaml_and_frontmatter(tmp_path):
    p = tmp_path / 'data.yaml'
    write_yaml(p, {'kurs': Decimal('0.1234567890123456789')})
    assert read_yaml(p)['kurs'] == Decimal('0.1234567890123456789')
    p.write_text('---\nkurs: 0.1234567890123456789\n---\nText\n')
    assert read_frontmatter(p)[0]['kurs'] == Decimal('0.1234567890123456789')


@pytest.mark.parametrize('text', ['betrag: 10.00\nbetrag: 20.00\n', 'a: {b: 1, b: 2}\n'])
def test_duplicate_yaml_keys_rejected(tmp_path, text):
    p = tmp_path / 'bad.yaml'
    p.write_text(text)
    with pytest.raises(FormatError, match='doppelter Schlüssel'):
        read_yaml(p)


@pytest.mark.parametrize('text', ['| A | A |\n| --- | --- |\n| 1 | 2 |', '| A | B |\n| --- |\n| 1 | 2 |'])
def test_ambiguous_columns_rejected(text):
    with pytest.raises(FormatError):
        parse_table(text)


def test_broken_journal_is_not_empty_book(tmp_path):
    book = make_book(tmp_path)
    api.post_entry(book, '2026-01-05', '6500', '1020', '45.80', 'Test')
    p = book.month_file(__import__('datetime').date(2026, 1, 5))
    text = p.read_text()
    p.write_text('\n'.join('| -- |' if line.startswith('| ---') else line for line in text.splitlines()))
    assert any(i.level == 'fehler' and 'Journaltabelle' in i.message for i in check.run(Book(book.root)))


def test_detached_rows_rejected(tmp_path):
    book = make_book(tmp_path)
    api.post_entry(book, '2026-01-05', '6500', '1020', '45.80', 'Test')
    p = next((book.root / 'journal').rglob('*.md'))
    with p.open('a') as f:
        f.write('\n| 2026-01-06 | 26-002 | Lost | 6500 | 1020 | 10.00 |\n')
    assert any('ausserhalb' in i.message for i in check.run(Book(book.root)))


def test_long_cell_does_not_reformat_existing_rows():
    t = MdTable(['Text', 'Betrag'], [{'Text': 'A', 'Betrag': '10.00'}])
    original = render_table(t).splitlines()
    t.rows.append({'Text': 'A very long new booking description', 'Betrag': '999999.00'})
    assert render_table(t).splitlines()[:3] == original


def test_atomic_replace_failure_preserves_file(tmp_path, monkeypatch):
    p = tmp_path / 'file'
    p.write_bytes(b'original')
    def fail(*args):
        raise OSError('disk error')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        atomic_write(p, b'new')
    assert p.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [p]


def test_invalid_plugin_write_preserves_uncommitted_files(tmp_path):
    book = make_book(tmp_path)
    p = book.root / 'notes.md'
    p.write_text('User edits')
    before = storage._state(book.root)
    def corrupt(b):
        (b.root / 'notes.md').write_text('plugin edits')
        write_yaml(b.root / 'allkvitt.yaml', {'firma': ''})
        return [b.root / 'notes.md', b.root / 'allkvitt.yaml']
    with pytest.raises(BookError):
        api.write(book, 'bad plugin', corrupt)
    assert storage._state(book.root) == before


def test_plugin_exception_rolls_back_all_files(tmp_path):
    book = make_book(tmp_path)
    before = storage._state(book.root)
    def broken(b):
        (b.root / 'new.txt').write_text('unfinished')
        (b.root / 'kontenplan.yaml').unlink()
        raise RuntimeError('crash')
    with pytest.raises(RuntimeError, match='crash'):
        api.write(book, 'broken', broken)
    assert storage._state(book.root) == before


def test_revision_and_idempotency(tmp_path):
    book = make_book(tmp_path)
    rev = api.status(book)['revision']
    args = ('2026-01-05', '6500', '1020', '45.80', 'Test')
    first = api.post_entry(book, *args, expected_revision=rev, idempotency_key='one')
    replay = api.post_entry(Book(book.root), *args, expected_revision=rev, idempotency_key='one')
    assert len(Book(book.root).rows) == 1
    assert replay['revision'] == first['revision'] and replay['wiederholt']
    with pytest.raises(BookError, match='andere Parameter'):
        api.post_entry(book, *args[:-1], 'Changed', idempotency_key='one')
    with pytest.raises(BookError, match='inzwischen geändert'):
        api.post_entry(book, *args, expected_revision=rev)
    rev = api.status(book)['revision']
    (book.root / 'note.md').write_text('external')
    with pytest.raises(BookError, match='inzwischen geändert'):
        api.post_entry(book, *args, expected_revision=rev)


def test_external_edit_during_preparation_survives(tmp_path):
    book = make_book(tmp_path)
    def update(b):
        (book.root / 'note.md').write_text('external')
        (b.root / 'note.md').write_text('ours')
        return [b.root / 'note.md']
    with pytest.raises(BookError, match='extern geändert'):
        api.write(book, 'conflict', update)
    assert (book.root / 'note.md').read_text() == 'external'


def test_account_extensions_and_format_upgrade(tmp_path):
    book = make_book(tmp_path)
    config = book.root / 'allkvitt.yaml'
    data = read_yaml(config)
    data.pop('format_version', None)
    write_yaml(config, data)
    chart = book.root / 'kontenplan.yaml'
    data = read_yaml(chart)
    data['extension'] = {'owner': 'test'}
    data['konten'][0]['custom'] = {'project': 'alpha'}
    number = str(data['konten'][0]['nr'])
    write_yaml(chart, data)
    assert Book(book.root).settings.get('format_version') is None
    api.add_account(book, '9998', 'Test', 'aufwand')
    assert read_yaml(config)['format_version'] == 1
    updated = read_yaml(chart)
    assert updated['extension'] == {'owner': 'test'}
    assert next(a for a in updated['konten'] if str(a['nr']) == number)['custom'] == {'project': 'alpha'}
    data = read_yaml(config)
    data['format_version'] = 999
    write_yaml(config, data)
    assert any('format_version' in i.message for i in check.run(Book(book.root)))


def test_recovery_after_process_killed_mid_publication(tmp_path):
    book = make_book(tmp_path)
    before = storage._state(book.root)
    script = '''
import os, sys
from pathlib import Path
from allkvitt import api, storage
from allkvitt.book import Book
root = Path(sys.argv[1])
original = storage.atomic_write
count = 0
def crash(path, data):
    global count
    original(path, data)
    if str(path).startswith(str(root)) and '/transaction/' not in str(path):
        count += 1
        if count == 1:
            os._exit(77)
storage.atomic_write = crash
api.post_entry(Book(root), '2026-01-05', '6500', '1020', '45.80', 'crash')
'''
    result = subprocess.run([sys.executable, '-c', script, str(book.root)])
    assert result.returncode == 77
    assert (book.root / '.allkvitt/transaction/manifest.json').exists()
    api.status(Book(book.root))
    assert storage._state(book.root) == before


def test_git_failure_restores_index_and_book(tmp_path, monkeypatch):
    from allkvitt import gitlog
    book = make_book(tmp_path)
    if not gitlog.is_repo(book.root):
        gitlog.init_repo(book.root)
        gitlog.commit(book.root, 'initial')
    p = book.root / 'notes.md'
    p.write_text('staged')
    gitlog._git(book.root, 'add', 'notes.md')
    p.write_text('unstaged')
    before = storage._state(book.root)
    index = (book.root / '.git/index').read_bytes()
    original = gitlog._git
    def fail(root, *args, **kwargs):
        if args[0] == 'commit':
            return subprocess.CompletedProcess(args, 1, '', 'rejected')
        return original(root, *args, **kwargs)
    monkeypatch.setattr(gitlog, '_git', fail)
    with pytest.raises(RuntimeError, match='rejected'):
        api.post_entry(book, '2026-01-05', '6500', '1020', '45.80', 'Test')
    assert storage._state(book.root) == before
    assert (book.root / '.git/index').read_bytes() == index


def test_recovery_keeps_commit_after_process_killed(tmp_path):
    book = make_book(tmp_path, git=True)
    script = '''
import os, sys
from pathlib import Path
from allkvitt import api, storage
from allkvitt.book import Book
root = Path(sys.argv[1])
original = storage.atomic_write_text
def crash(path, data):
    if path.name == 'manifest.json' and '"phase": "committed"' in data:
        os._exit(78)
    return original(path, data)
storage.atomic_write_text = crash
api.post_entry(Book(root), '2026-01-05', '6500', '1020', '45.80', 'committed', idempotency_key='crash-key')
'''
    result = subprocess.run([sys.executable, '-c', script, str(book.root)])
    assert result.returncode == 78
    api.status(Book(book.root))
    assert len(Book(book.root).rows) == 1
    replay = api.post_entry(Book(book.root), '2026-01-05', '6500', '1020', '45.80', 'committed', idempotency_key='crash-key')
    assert replay['wiederholt'] and replay['commit']
    assert not (book.root / '.allkvitt/transaction').exists()


def test_reader_waits_for_writer(tmp_path):
    import threading
    book = make_book(tmp_path)
    started = threading.Event()
    finished = threading.Event()
    errors = []
    def reader():
        started.set()
        try:
            api.status(Book(book.root))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    with storage.locked(book.root):
        thread = threading.Thread(target=reader)
        thread.start()
        assert started.wait(2)
        assert not finished.wait(.1)
    thread.join(5)
    assert finished.is_set() and not errors


def test_agent_tools_retry_and_proposal_permission(tmp_path, monkeypatch):
    from allkvitt import tools
    book = make_book(tmp_path)
    monkeypatch.setenv('ALLKVITT_BUCH', str(book.root))
    args = ('2026-01-05', '6500', '1020', '45.80', 'Test')
    assert not tools.book_entry(*args, idempotency_key='denied')['ok']
    rev = tools.status()['revision']
    first = tools.propose_booking(*args, 'Papier', expected_revision=rev, idempotency_key='proposal')
    second = tools.propose_booking(*args, 'Papier', expected_revision=rev, idempotency_key='proposal')
    assert first['ok'] and second['wiederholt']
    assert len(api.proposals(book)) == 1
    assert not Book(book.root).rows


def test_recovery_preserves_external_conflict(tmp_path):
    book = make_book(tmp_path)
    p = book.root / 'note.md'
    p.write_text('old')
    folder = book.root / '.allkvitt/transaction'
    backup = folder / 'before/note.md'
    atomic_write(backup, b'old')
    manifest = {'version': 1, 'id': 'test', 'phase': 'prepared', 'head': None, 'repo': False,
                'index': False, 'files': {'note.md': {'old': storage._hash(p),
                          'new': __import__('hashlib').sha256(b'new').hexdigest()}}}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    p.write_text('external change')
    with pytest.raises(FormatError, match='extern geändert'):
        api.status(Book(book.root))
    assert p.read_text() == 'external change'
    assert backup.read_bytes() == b'old'


def test_absolute_receipt_path_is_staged_but_booking_text_is_not(tmp_path):
    book = make_book(tmp_path)
    receipt = book.root / 'inbox/test.txt'
    receipt.write_text('receipt')
    literal_text = str(book.root / 'this is a description')
    api.post_entry(book, '2026-01-05', '6500', '1020', '45.80', literal_text, datei=str(receipt))
    assert Book(book.root).rows[0].text == literal_text
    assert not receipt.exists()
    assert next((book.root / 'belege').rglob('*.txt')).read_text() == 'receipt'


@pytest.mark.parametrize('same_key', [False, True])
def test_parallel_processes_serialize_and_deduplicate(tmp_path, same_key):
    book = make_book(tmp_path)
    script = '''
import json, sys
from pathlib import Path
from allkvitt import api
from allkvitt.book import Book
result = api.post_entry(Book(Path(sys.argv[1])), '2026-01-05', '6500', '1020', '45.80',
                        'parallel', idempotency_key=sys.argv[2])
print(json.dumps(result))
'''
    processes = [subprocess.Popen([sys.executable, '-c', script, str(book.root), key],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for key in ('first', 'first' if same_key else 'second')]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert len(Book(book.root).rows) == (1 if same_key else 2)
    assert sum(bool(r.get('wiederholt')) for r in results) == int(same_key)


def test_transaction_keeps_generated_files_out_of_git(tmp_path):
    from allkvitt import gitlog
    book = make_book(tmp_path, git=True)
    def generate(b):
        note = b.root / 'note.md'
        note.write_text('User document')
        cache = b.root / '.allkvitt/erfassung/ocr.txt'
        cache.parent.mkdir(parents=True)
        cache.write_text('OCR cache')
        return [note]
    result = api.write(book, 'document with cache', generate)
    assert result['commit']
    assert (book.root / '.allkvitt/erfassung/ocr.txt').read_text() == 'OCR cache'
    tracked = gitlog._git(book.root, 'ls-files').stdout.splitlines()
    assert 'note.md' in tracked
    assert '.allkvitt/erfassung/ocr.txt' not in tracked
