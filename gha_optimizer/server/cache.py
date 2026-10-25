import json
import logging
from typing import Any, Optional
import redis

from gha_optimizer.config import config

logger = logging.getLogger(__name__)

# Initialize redis connection
redis_client: Optional[Any] = None
try:
    redis_client = redis.from_url(config.redis_url, decode_responses=True)
except Exception as e:
    logger.warning("Failed to initialize Redis client: %s", e)
    redis_client = None

def get_cache(key: str) -> Optional[Any]:
    """Retrieve an item from the cache."""
    if not redis_client:
        return None
    try:
        val = redis_client.get(key)
        return json.loads(val) if val else None
    except Exception as e:
        logger.warning("Cache GET failed for %s: %s", key, e)
        return None

def set_cache(key: str, value: Any, ttl_seconds: int = 3600) -> bool:
    """Set an item in the cache with a TTL."""
    if not redis_client:
        return False
    try:
        redis_client.setex(key, ttl_seconds, json.dumps(value))
        return True
    except Exception as e:
        logger.warning("Cache SET failed for %s: %s", key, e)
        return False

def invalidate_cache(key_pattern: str) -> bool:
    """Invalidate cache keys matching a pattern."""
    if not redis_client:
        return False
    try:
        keys = redis_client.keys(key_pattern)
        if keys:
            redis_client.delete(*keys)
        return True
    except Exception as e:
        logger.warning("Cache INVALIDATE failed for %s: %s", key_pattern, e)
        return False
