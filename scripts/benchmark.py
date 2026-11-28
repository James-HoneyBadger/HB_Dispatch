#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Reproducible synthetic-archive benchmarks.

Builds a deterministic synthetic archive in a temporary directory by
default, then measures ingest, FTS search, timeline paging and dashboard
queries. It never touches a real archive: an explicit work directory must be
empty, and temporary directories are removed unless --keep is given.

Record the printed JSON (or --json output) with the machine description so
results from different computers are not confused.
"""
import argparse, json, os, platform, random, shutil, statistics, sys, tempfile, time

try:
    import resource
except ImportError:  # Windows has no POSIX resource module.
    resource = None
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hb_engine.dispatch import Archive, timeline_rows  # noqa: E402
from hb_engine.ipc import _dashboard  # noqa: E402
from hb_engine.unified import search_unified  # noqa: E402

WORDS = (
    'archive preserve history record document photograph journal letter memory '
    'project research correspondence timeline subject evidence provenance '
    'network platform export import search index source original derived '
    'invoice receipt travel family work engineering design decision meeting '
    'synthetic sample fixture determinism measurement throughput latency'
).split()

def generate_messages(count, seed=20261127):
    """Yield deterministic, unique synthetic RFC 2822 messages."""
    rng = random.Random(seed)
    base = datetime(2005, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        when = base + timedelta(minutes=index * 37)
        body = ' '.join(rng.choice(WORDS) for _ in range(40))
        yield (
            f'Subject: Synthetic {index}\r\n'
            f'From: sender{index % 250}@example.invalid\r\n'
            f'Date: {when.strftime("%a, %d %b %Y %H:%M:%S +0000")}\r\n'
            f'Message-ID: <{index}@synthetic.invalid>\r\n\r\n'
            f'{body}\r\n'
        ).encode()

def _timed(call):
    start = time.perf_counter()
    result = call()
    return time.perf_counter() - start, result

def _percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {'p50_ms': None, 'p95_ms': None}
    def at(fraction):
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index] * 1000, 1)
    return {'p50_ms': at(0.5), 'p95_ms': at(0.95),
            'mean_ms': round(statistics.fmean(ordered) * 1000, 1)}

def _storage(root):
    root = Path(root)
    raw_bytes = sum(p.stat().st_size for p in (root / 'raw').glob('*.eml')
                    if p.is_file() and not p.is_symlink())
    db_bytes = sum(p.stat().st_size for p in root.glob('*.sqlite3*') if p.is_file())
    return {'raw_bytes': raw_bytes, 'database_bytes': db_bytes,
            'total_bytes': raw_bytes + db_bytes}

def prepare_workdir(path):
    """An explicit work directory must be empty; temporary dirs are preferred."""
    target = Path(path).expanduser().resolve()
    if target.exists():
        if not target.is_dir():
            raise SystemExit(f'work directory is not a directory: {target}')
        if any(target.iterdir()):
            raise SystemExit(f'work directory must be empty: {target}')
    else:
        target.mkdir(parents=True)
    return target

def run_size(count, base=None, seed=20261127, health=False, cleanup=False):
    """Build one synthetic archive and return its measurements."""
    root = Path(tempfile.mkdtemp(prefix=f'hb-bench-{count}-', dir=str(base) if base else None))
    archive = Archive(root)
    result = {'messages': count, 'root': str(root)}
    try:
        started = time.perf_counter()
        for message in generate_messages(count, seed):
            archive.ingest(message)
        elapsed = time.perf_counter() - started
        result['ingest'] = {
            'seconds': round(elapsed, 3),
            'messages_per_second': round(count / elapsed, 1) if elapsed else None,
        }
        result['storage'] = _storage(root)

        queries = [WORDS[(i * 7) % len(WORDS)] for i in range(20)]
        find_times = [_timed(lambda q=q: archive.find(q, limit=50))[0] for q in queries]
        result['find'] = _percentiles(find_times)
        unified_times = [_timed(lambda q=q: search_unified(root, q, limit=50))[0]
                         for q in queries]
        result['unified_search'] = _percentiles(unified_times)

        first, rows_first = _timed(lambda: timeline_rows(archive.db, limit=100))
        deep_offset = max(0, count - 100)
        deep, rows_deep = _timed(
            lambda: timeline_rows(archive.db, limit=100, offset=deep_offset))
        reverse, rows_reverse = _timed(
            lambda: timeline_rows(archive.db, limit=100, order='desc'))
        result['timeline'] = {
            'first_page_ms': round(first * 1000, 1), 'first_page_rows': len(rows_first),
            'deep_page_ms': round(deep * 1000, 1), 'deep_offset': deep_offset,
            'deep_page_rows': len(rows_deep),
            'reverse_page_ms': round(reverse * 1000, 1),
            'reverse_page_rows': len(rows_reverse),
        }
        dashboard_time, dashboard = _timed(lambda: _dashboard(root))
        result['dashboard'] = {'ms': round(dashboard_time * 1000, 1),
                               'by_year_buckets': len(dashboard['by_year']),
                               'undated': dashboard['archive']['undated']}
        if health:
            health_time, report = _timed(lambda: archive.health())
            result['health'] = {'seconds': round(health_time, 3),
                                'consistent': bool(report['sqlite_ok'])}
    finally:
        archive.db.close()
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)
    result['max_rss_mib'] = max_rss_mib()
    return result

def max_rss_mib():
    """Peak resident memory, or None where the platform cannot report it."""
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 * 1024) if sys.platform == 'darwin' else value / 1024, 1)

def machine_description():
    return {'platform': platform.platform(), 'machine': platform.machine(),
            'processor': platform.processor(), 'python': platform.python_version(),
            'cpu_count': os.cpu_count()}

def render_table(results):
    header = ('messages', 'ingest_s', 'msg/s', 'raw_MiB', 'db_MiB', 'find_p50_ms',
              'find_p95_ms', 'timeline_first_ms', 'timeline_deep_ms', 'dashboard_ms',
              'max_rss_MiB')
    lines = ['| ' + ' | '.join(header) + ' |',
             '|' + '|'.join('---' for _ in header) + '|']
    for item in results:
        mib = 1024 * 1024
        lines.append('| ' + ' | '.join(str(value) for value in (
            item['messages'],
            item['ingest']['seconds'],
            item['ingest']['messages_per_second'],
            round(item['storage']['raw_bytes'] / mib, 1),
            round(item['storage']['database_bytes'] / mib, 1),
            item['find']['p50_ms'], item['find']['p95_ms'],
            item['timeline']['first_page_ms'], item['timeline']['deep_page_ms'],
            item['dashboard']['ms'],
            item['max_rss_mib'] if item['max_rss_mib'] is not None else 'n/a')) + ' |')
    return '\n'.join(lines)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', default='10000',
                        help='comma-separated record counts (default 10000)')
    parser.add_argument('--workdir', help='empty directory for synthetic archives')
    parser.add_argument('--keep', action='store_true', help='keep temporary archives')
    parser.add_argument('--health', action='store_true', help='also time a full health scan')
    parser.add_argument('--seed', type=int, default=20261127)
    parser.add_argument('--json', help='write the JSON result to this new file')
    args = parser.parse_args(argv)
    sizes = [int(value) for value in args.sizes.split(',') if value.strip()]
    if not sizes or any(size < 1 or size > 1_000_000 for size in sizes):
        raise SystemExit('sizes must be between 1 and 1,000,000')
    if args.json and Path(args.json).exists():
        raise SystemExit(f'benchmark: refusing to replace {args.json}')
    base = prepare_workdir(args.workdir) if args.workdir else None
    created = None
    if base is None:
        created = Path(tempfile.mkdtemp(prefix='hb-bench-'))
        base = created
    try:
        results = [run_size(size, base, args.seed, args.health) for size in sizes]
        document = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'machine': machine_description(), 'results': results}
        text = json.dumps(document, indent=2)
        print(render_table(results))
        print()
        print(text)
        if args.json:
            try:
                with open(args.json, 'x', encoding='utf-8') as handle:
                    handle.write(text + '\n')
            except FileExistsError:
                print(f'benchmark: refusing to replace {args.json}', file=sys.stderr)
                return 2
    finally:
        if created is not None and not args.keep:
            shutil.rmtree(created, ignore_errors=True)
        elif created is not None:
            print(f'kept synthetic archives under {created}', file=sys.stderr)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
