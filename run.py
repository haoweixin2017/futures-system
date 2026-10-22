import asyncio
import sys

import uvicorn

if __name__ == '__main__':
    from backend.common.config import cfg
    from scripts.startup_dependencies import ensure_dependencies

    try:
        ensure_dependencies(cfg)
    except RuntimeError as exc:
        print(f'[启动失败] {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
    loop = asyncio.SelectorEventLoop if sys.platform == 'win32' else 'auto'
    uvicorn.run('backend.main:app', host='127.0.0.1', port=8000, reload=False, loop=loop)
