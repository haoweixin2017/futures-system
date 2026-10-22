import redis
from backend.common.config import cfg

class RedisClient:
    _instance = None

    @classmethod
    def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            # 只有当非空时才传 password，避免 Redis 无密码时发送 AUTH 报错
            pwd = cfg.redis.password if (cfg.redis.password and cfg.redis.password.strip()) else None
            cls._instance = redis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=pwd,
                decode_responses=True,
                protocol=2
            )
        return cls._instance
