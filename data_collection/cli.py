import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .scheduler import run_dates, self_check


def main(argv=None):
    parser = argparse.ArgumentParser(description='内部研究仓单和日行情采集')
    parser.add_argument('--config', default='data_collection/config.yaml')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'backfill'):
        cmd = commands.add_parser(name)
        dates = cmd.add_mutually_exclusive_group(required=True)
        dates.add_argument('--date')
        dates.add_argument('--range', nargs=2, metavar=('START', 'END'))
        cmd.add_argument('--exchange', choices=['CZCE', 'DCE'])
        cmd.add_argument('--dry-run', action='store_true')
        cmd.add_argument('--force', action='store_true', help='重新检查已成功的日期，幂等替换仓单快照')
    commands.add_parser('self-check')
    commands.add_parser('schedule')
    args = parser.parse_args(argv)
    try:
        # Explicit local secrets file; values are never printed. Existing environment wins.
        import os
        env_file = Path(__file__).resolve().parents[1] / '.env'
        if env_file.exists():
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
        config = load_config(args.config)
        if args.command == 'self-check':
            reports = self_check(config)
            print(json.dumps(reports, ensure_ascii=False, indent=2))
            return 2 if any(r['status'] == 'FAILED' for r in reports) else 0
        if args.command == 'schedule':
            from .scheduler import serve
            serve(config)
            return 0
        start, end = (args.date, args.date) if args.date else args.range
        reports = run_dates(config, start, end, args.exchange, args.dry_run, args.force)
        print(json.dumps({'dry_run': args.dry_run, 'results': reports}, ensure_ascii=False, indent=2))
        return 1 if any(r['status'] in ('FAILED', 'REJECTED') for r in reports) else 0
    except (ValueError, OSError) as exc:
        from .scheduler import reason
        print(json.dumps({'status': 'SELF_CHECK_FAILED', 'reason': reason(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
