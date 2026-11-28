#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Demonstrate practical research over a small fictional multi-year archive.

The archive is created in a temporary directory unless --keep is used. It
contains synthetic correspondence from 2019-2023 and two local documents, so
the demo never touches the owner's archive or any external service.

Retrieval-only runs need no model. --ask performs an explicitly enabled local
Ollama request against the loopback endpoint supplied on the command line.
"""
import argparse
import os
import sys
import tempfile
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hb_engine.dispatch import Archive  # noqa: E402
from hb_engine.documents import import_files  # noqa: E402
from hb_engine.intelligence import Intelligence, OllamaClient  # noqa: E402

MESSAGES = (
    ('2019-03-04', 'alice@example.invalid', 'Silver invoice',
     'I paid the silver invoice for the workshop shelves. The amount was 100 dollars.'),
    ('2019-03-12', 'bob@example.invalid', 'Re: Silver invoice',
     'The silver invoice was cancelled, although the amount recorded was 200 dollars.'),
    ('2020-07-20', 'alice@example.invalid', 'Garden plans',
     'The garden plans include tomatoes, beans and a new herb bed.'),
    ('2021-11-02', 'dana@example.invalid', 'Heritage grant application',
     'The heritage grant application was submitted to the county heritage fund.'),
    ('2022-01-15', 'bob@example.invalid', 'Heritage grant result',
     'The county heritage fund rejected the heritage grant application.'),
    ('2023-05-09', 'alice@example.invalid', 'Silver archive note',
     'I found the old silver invoice while sorting the workshop archive.'),
)

DOCUMENTS = (
    ('garden-notes.md', '2020-07-21',
     '# Garden notes\n\nBeans were planted beside the shed. Tomatoes were watered daily.\n'),
    ('heritage-grant-notes.txt', '2022-01-20',
     'Heritage grant notes: the application was rejected by the county fund. '
     'A revised application was not recorded in this fictional archive.\n'),
)


def _synthetic_message(sender, subject, date, body):
    when = datetime.fromisoformat(date + 'T10:00:00+00:00')
    return (
        f'From: {sender}\r\nSubject: {subject}\r\n'
        f'Date: {format_datetime(when)}\r\n'
        f'Message-ID: <{date}-{subject.replace(" ", "-")}@demo.invalid>\r\n\r\n'
        f'{body}\r\n'
    ).encode('utf-8')


def build_archive(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    archive = Archive(root)
    try:
        for date, sender, subject, body in MESSAGES:
            archive.ingest(_synthetic_message(sender, subject, date, body))
    finally:
        archive.db.close()
    documents = root / 'documents'
    documents.mkdir()
    for name, date, text in DOCUMENTS:
        path = documents / name
        path.write_text(text, encoding='utf-8')
        stamp = datetime.fromisoformat(date + 'T12:00:00+00:00').timestamp()
        os.utime(path, (stamp, stamp))
    import_files([str(documents)], root)
    return root


def _print_retrieval(label, result):
    print(f'\n## {label}')
    print('queries:', ' | '.join(result['queries']) or '(none)')
    print('context:', result['context']['characters'], '/',
          result['context']['budget'], 'characters;',
          'max input:', result['context']['max_input'])
    if result['retrieval'].get('history_fallback'):
        print('retrieval used the previous question as a follow-up fallback')
    if not result['evidence']:
        print('no matching records in this scope')
        return
    for item in result['evidence']:
        print(f"{item['evidence_id']}  {item['date']}  {item['source']}  "
              f"{item['title']}")
        print(f"    {item['source_location']}")
        print('    ' + item['excerpt'].replace('\n', ' ')[:180])


def run_demo(engine, args):
    first = engine.retrieve({'question': 'What happened with the silver invoice?',
                             'depth': 12, 'per_record_chars': 600})
    _print_retrieval('Silver invoice', first)
    followup = engine.retrieve({
        'question': 'What happened next?',
        'history': [{'role': 'user',
                     'content': 'What happened with the silver invoice?'}],
        'depth': 12, 'per_record_chars': 600,
    })
    _print_retrieval('Follow-up without repeating the topic', followup)
    _print_retrieval('Heritage grant', engine.retrieve({
        'question': 'What happened with the heritage grant application?',
        'depth': 12, 'per_record_chars': 600}))
    _print_retrieval('Garden document', engine.retrieve({
        'question': 'What did the garden notes say about beans?',
        'sources': ['document'], 'depth': 12, 'per_record_chars': 600}))
    _print_retrieval('Unrecorded refund', engine.retrieve({
        'question': 'Was a final refund recorded?',
        'depth': 12, 'per_record_chars': 600}))
    if not args.ask:
        print('\nRetrieval-only demo complete. Use --ask --model NAME to try local Ollama.')
        return
    prefs = {
        'ai_enabled': True, 'ai_backend': 'ollama',
        'ai_endpoint': args.endpoint, 'ai_model': args.model,
        'ai_timeout': args.timeout, 'ai_max_input_chars': 12_000,
    }
    client = OllamaClient(engine.root, prefs)
    if not client.status(args.model).get('reachable'):
        print('\nOllama is not reachable; retrieval remains available without a model.')
        return
    answer = Intelligence(engine.root, prefs=prefs, client=client).ask({
        'question': 'What happened with the silver invoice?',
        'depth': 12, 'per_record_chars': 600, 'model': args.model,
    })
    print('\n## Model answer')
    print(answer.get('answer') or answer.get('error') or '(no answer)')
    print('citations:', ', '.join(answer.get('citations') or []) or '(none)')
    print('grounded:', answer.get('grounded'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep', action='store_true',
                        help='keep the synthetic archive and print its path')
    parser.add_argument('--ask', action='store_true',
                        help='opt in to a local Ollama request')
    parser.add_argument('--endpoint', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='llama3.2')
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    root = Path(tempfile.mkdtemp(prefix='hb-intelligence-demo-'))
    try:
        build_archive(root)
        print('Synthetic historical archive:', root)
        print('All names and events are fictional and generated for this demo.')
        run_demo(Intelligence(root, prefs={'ai_model': args.model,
                                      'ai_max_input_chars': 12_000}), args)
    finally:
        if args.keep:
            print('\nkept:', root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
