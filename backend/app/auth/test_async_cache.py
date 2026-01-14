import asyncio

import pytest
from app.auth.async_cache import AsyncTTLCache


class TestAsyncTTLCache:
    @pytest.mark.asyncio
    async def test_basic_get_set(self) -> None:
        """Test basic cache get/set operations."""
        cache = AsyncTTLCache(ttl=10, max_size=100)

        # Test set and get
        await cache.set("key1", "value1")
        result = await cache.get("key1")
        assert result == "value1"

        # Test get non-existent key
        result = await cache.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self) -> None:
        """Test that cache entries expire after TTL."""
        cache = AsyncTTLCache(ttl=0.1, max_size=100)  # 0.1 second TTL

        await cache.set("key1", "value1")

        # Should exist immediately
        result = await cache.get("key1")
        assert result == "value1"

        # Wait for expiration
        await asyncio.sleep(0.2)

        # Should be expired
        result = await cache.get("key1")
        assert result is None

    @pytest.mark.asyncio
    async def test_max_size_lru_eviction(self) -> None:
        """Test LRU eviction when cache reaches max size."""
        cache = AsyncTTLCache(ttl=10, max_size=3)

        # Fill cache to max size
        await cache.set("key1", "value1")
        await cache.set("key2", "value2")
        await cache.set("key3", "value3")

        # All should exist
        assert await cache.get("key1") == "value1"
        assert await cache.get("key2") == "value2"
        assert await cache.get("key3") == "value3"

        # Add one more - should evict oldest (key1)
        await cache.set("key4", "value4")

        # key1 should be evicted
        assert await cache.get("key1") is None
        assert await cache.get("key2") == "value2"
        assert await cache.get("key3") == "value3"
        assert await cache.get("key4") == "value4"

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        """Test clearing the cache."""
        cache = AsyncTTLCache(ttl=10, max_size=100)

        # Add some entries
        await cache.set("key1", "value1")
        await cache.set("key2", "value2")

        # Verify they exist
        assert await cache.get("key1") == "value1"
        assert await cache.get("key2") == "value2"

        # Clear cache
        await cache.clear()

        # Verify all cleared
        assert await cache.get("key1") is None
        assert await cache.get("key2") is None

    @pytest.mark.asyncio
    async def test_concurrent_access(self) -> None:
        cache = AsyncTTLCache(ttl=10, max_size=100)

        async def set_values(start: int, count: int) -> None:
            for i in range(start, start + count):
                await cache.set(f"key{i}", f"value{i}")

        async def get_values(start: int, count: int) -> list[str]:
            results = []
            for i in range(start, start + count):
                result = await cache.get(f"key{i}")
                if result:
                    results.append(result)
            return results

        # Concurrent sets
        await asyncio.gather(
            set_values(0, 10),
            set_values(10, 10),
            set_values(20, 10),
        )

        # Concurrent gets
        results = await asyncio.gather(
            get_values(0, 10),
            get_values(10, 10),
            get_values(20, 10),
        )

        # Verify all values were set correctly
        all_results = []
        for result_list in results:
            all_results.extend(result_list)

        assert len(all_results) == 30
        assert all(f"value{i}" in all_results for i in range(30))

    @pytest.mark.asyncio
    async def test_update_existing_key(self) -> None:
        """Test updating an existing key resets its expiry."""
        cache = AsyncTTLCache(ttl=0.5, max_size=100)  # 0.5 second TTL

        # Set initial value
        await cache.set("key1", "value1")

        # Wait half the TTL
        await asyncio.sleep(0.3)

        # Update the value - should reset expiry
        await cache.set("key1", "value2")

        # Wait another 0.3 seconds (total 0.6 seconds from original set)
        await asyncio.sleep(0.3)

        # Should still exist with new value because expiry was reset
        result = await cache.get("key1")
        assert result == "value2"

        # Wait for new expiry
        await asyncio.sleep(0.3)

        # Now should be expired
        result = await cache.get("key1")
        assert result is None
