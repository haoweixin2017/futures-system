"""单元测试隔离引导模块 (幂等)。

任何测试文件在导入后端模块之前先 `import test_bootstrap`,
即可自动切换到隔离环境, 避免污染生产数据库:
- MySQL 重绑定到独立随机测试库 citic_futures_test_* (自动建库建表)
- Redis 自动选择空库；绝不清空已有数据
"""

_isolated = False


def setup():
    global _isolated
    if _isolated:
        return

    from backend.common.config import cfg

    import uuid
    import redis
    import atexit
    production_db = cfg.redis.db
    for candidate in range(15, 0, -1):
        if candidate == production_db:
            continue
        isolated = redis.Redis(host=cfg.redis.host, port=cfg.redis.port,
                               password=cfg.redis.password or None, db=candidate, protocol=2)
        if isolated.dbsize() == 0:
            cfg.redis.db = candidate
            break
    else:
        raise RuntimeError("没有空闲 Redis 测试库，拒绝清空既有数据")
    atexit.register(isolated.flushdb)

    from backend.storage.mysql_client import rebind_engine, init_db

    rebind_engine("citic_futures_test_" + uuid.uuid4().hex[:12])
    init_db()

    from backend.storage.redis_client import RedisClient
    RedisClient._instance = None

    try:
        RedisClient.get_client().flushdb()
    except Exception:
        pass

    from unittest.mock import patch
    # 订阅接口的后台回补另做集成验收，单元测试不得访问公网或并发灌入历史行情。
    start_backfill_patch()
    _isolated = True


_backfill_patch = None
_backfill_active = False


def start_backfill_patch():
    """屏蔽后台回填入口 (幂等)。随 setup() 全局生效, 防止任何测试触发公网回补。"""
    global _backfill_patch, _backfill_active
    if _backfill_patch is None:
        from unittest.mock import patch
        _backfill_patch = patch("backend.storage.history_backfill.ensure_symbol_backfilled")
    if not _backfill_active:
        _backfill_patch.start()
        _backfill_active = True


def pause_backfill_patch():
    """临时还原真实 ensure_symbol_backfilled, 供需要测真实回填逻辑的模块使用。
    在该模块的 setUpModule 中调用, tearDownModule 中必须 resume_backfill_patch()。"""
    global _backfill_active
    if _backfill_patch is not None and _backfill_active:
        _backfill_patch.stop()
        _backfill_active = False


def resume_backfill_patch():
    global _backfill_active
    if _backfill_patch is not None and not _backfill_active:
        _backfill_patch.start()
        _backfill_active = True


setup()

# 统一两种导入形态的模块身份: 无论以 test_bootstrap 还是 tests.test_bootstrap 首次导入,
# 都注册为同一模块对象。否则 setup() 会执行两次, 第二次另选一个空闲 Redis 库并重建客户端,
# 而 routes 等模块在两次之间导入时捕获的 redis_client 仍指向旧库, 造成跨库读写互不可见。
import sys as _sys
_me = _sys.modules[__name__]
_alias = "tests.test_bootstrap" if __name__ == "test_bootstrap" else "test_bootstrap"
if _alias not in _sys.modules:
    _sys.modules[_alias] = _me
